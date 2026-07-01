# Cancel and kill-switch: CancelSignal by signal_id, a cancel_requested marker, a halt-only kill-switch

This ADR fixes the two control-plane mechanics ADR-0007 and ADR-0017 left open: how a cancel is
expressed and resolved, and how the kill-switch behaves.

## A cancel is a `CancelSignal` that targets the original `signal_id`

`Signal` is a typed hierarchy (ADR-0025): `PlaceSignal` (side, qty, price, order type, TIF,
`post_only`, per ADR-0012) and `CancelSignal`. A `CancelSignal` carries its **own** seq'd
`signal_id` — a cancel is itself a fresh, replayable intent — plus **`target_signal_id`**, the
original order it cancels. The strategy references orders by the `signal_id` **it** emitted; the
`ExecutionManager` re-derives the target `cloid` (the same deterministic derivation, ADR-0006) and
issues `exchange.cancel(cloid)`. Carrying `target_cloid` directly was rejected: it leaks the engine's
`cloid` derivation into every strategy author's code. Idempotency falls out of ADR-0025 — a re-emitted
`CancelSignal` has the same `signal_id` and dedupes; cancelling an already-terminal order is a no-op.

## In-flight cancels resolve via a `cancel_requested` marker, not a `CANCELLING` state

ADR-0007 models cancel as `LIVE → CANCELLED` with **no `CANCELLING` state**. But a cancelled order
vanishes from venue open-orders, which is indistinguishable from a **ghost** (→ `REJECTED`,
ADR-0011) unless we record intent — and collapsing `CANCELLED` into `REJECTED` violates ADR-0010.

So on a `CancelSignal` the `ExecutionManager` checkpoints a **`cancel_requested`** flag+timestamp on
the `LIVE` saga record, *then* calls `exchange.cancel(cloid)`. This is a marker, **not** an FSM state:
the order stays `LIVE` and can still fill (the cancel/fill race is real). Resolution:

- **Happy path** — venue cancel-ack (`OrderStatusReport` cancelled) → `LIVE → CANCELLED`.
- **Lost-ack / crash path** — reconciliation finds the order gone and cross-checks fill history
  (ADR-0011 inv 2/4): fills found → `FILLED`/`PARTIALLY_FILLED` (cancel lost the race); no new fills
  **and `cancel_requested`** → `CANCELLED` (our cancel landed, ack lost); no new fills **and not
  `cancel_requested`** → `REJECTED` (ghost, unsolicited disappearance).

`FILLED` is terminal and wins the race; a late cancel-ack on a terminal saga is an idempotent no-op
(ADR-0025); a cancel on an already-terminal/unknown order yields a benign venue "not found" report the
saga ignores. The retry-budget-below-grace timing invariant (ADR-0011 inv 7) still applies. Not
tracking intent (treat every vanished order as `REJECTED`-ghost) was rejected — it mislabels every
ack-lost cancel as a venue rejection. The marker is what makes "no `CANCELLING` state" correct rather
than lossy.

## The kill-switch is global and halt-only, tripped manually

The `PreTradeGuard` kill-switch (ADR-0017) is **halt-only**: tripped, the guard returns `DENIED`
(ADR-0010, never sent) for every new `PlaceSignal`; **resting `LIVE` orders are left untouched**. This
keeps it a pre-trade gate and preserves the "engine never auto-cancels" symmetry of ADR-0024.
Flatten (mass-cancel) is a louder, riskier operator action that already has a clean home on the
`CancelSignal` path, so it stays **separate and deferred**, not welded to a boolean. Scope is
**global** for v1 (one flag halts all new orders); per-strategy kill (halt one misbehaving strategy
under multi-strategy, ADR-0018) is a cheap additive extension — the guard already runs per-signal and
knows `strategy_id` — but ships deferred.

**Tripped manually only.** `trip_kill_switch(reason)` / `reset_kill_switch()` are callable
programmatically (tests) and wired to `SIGUSR1` by the runner, alongside the `SIGINT`/`SIGTERM`
handlers of ADR-0024 — a dependency-free operator interface, no HTTP admin surface in v1. Each
trip/reset emits a named event (`guard.kill_switch_tripped` / `guard.kill_switch_reset`, ADR-0020)
with the reason. **Automatic circuit-breaker tripping** (on consecutive `FAILED`s, a reconcile freeze,
a rejection storm) is deferred: those encode a *risk policy*, and a reference core exposes the
kill-switch mechanism, not an opinionated trip policy that ADR-0017 already fenced off as
not-a-RiskEngine.

**The kill-switch is durable (sticky).** Its state is persisted to the `Store` and restored during
recovery **before the feed starts** (ADR-0024), and it is cleared **only** by an explicit
`reset_kill_switch()`. This is fail-safe: an operator who halts trading because something is wrong
must not have the engine silently resume trading after a crash/restart. A tripped engine comes back
tripped — the startup-reconciliation barrier already gates trading, and the durable flag ensures a
halt outlives the process that set it. A runtime-only flag (cleared on restart) was rejected: it
turns any crash into an involuntary un-halt.
