# Engine runner: ordered startup barrier, crash-only supervision, origin-based error containment

ADR-0014 fixed the *component* contract (`READY→RUNNING→STOPPED`+`FAULTED`, `start`/`stop`,
crash-only, two error classes). This ADR fixes the `Engine` **host** that wires and sequences
those components.

## Startup is an ordered sequence gated on a reconciliation barrier

`Engine.start()`:

1. Bind the `run_id` correlation, init observability, open the `Store`, build the `Cache`.
2. **Recover**: rebuild the `Cache` from the `Store` (write-through projection, ADR-0009).
   **(Extended by ADR-0043 §6/§10:** the accounting ledger recovers here too, and it goes **first**
   — `Engine._start_sequence` calls `PortfolioProjection.recover()`, which checks the store's
   account binding, seeds the **paper** account row when the store has none, and then restores the
   ledger rows, *before* this `Cache` rebuild and before any other recovery work. The order is
   load-bearing twice over: the check can **refuse** the store outright (`StoreAccountMismatch`,
   ADR-0042 §3/ADR-0043 §8), which must happen before the engine has done work it would have to
   unwind; and its existence question — does `orders` hold rows with no account row? — is answered
   by a narrow `has_orders()` precisely so it need not run the mass read this step performs. The
   seed sits here rather than at step 5 because paper's opening value is *declared* config the
   check already holds, so it needs no venue read and cannot fail on connectivity; live's is
   ingested, which is why its twin waits for the barrier.**)**
3. Start the `EventBus` (InMemory: no-op; Kafka: connect consumers/producers).
4. Connect the `Exchange` + `ExecutionManager` (WS/HTTP; subscribe to `Signal`/`ExecutionReport`).
   **(Extended by ADR-0044 §7:** the *connect* half this step has always named now exists —
   `Exchange.start()`, declared on the Protocol (the `start()` ADR-0014 already assigns the
   component). It is the connect half **only**: this step awaits it inline and it must return so the
   barrier can run at step 5, so an adapter's long-lived loop belongs to `Exchange.run()`, which the
   TaskGroup below supervises ([#226](https://github.com/MarcosACH/tickwright/issues/226)).
   On **paper** it validates the configured leverage against `InstrumentSpec.max_leverage`
   and writes nothing; on **live** it validates, reads `clearinghouseState` once, and pushes the
   per-symbol leverage map to the venue via `updateLeverage` — the complete map the composition
   root resolved from the sparse `AppConfig.leverage` and the strategy-declared symbols
   (ADR-0044 §2), so the adapter needs no knowledge of strategies — skipping symbols already
   aligned, writing blind where no position is held, and raising **`VenueLeverageMismatch`** where
   config disagrees with a symbol that *does* hold one. That refusal is the venue twin of step 2's
   `StoreAccountMismatch`, and the order of the two is load-bearing: the local, cheap store check
   refuses first, the networked venue check second, **both before** the step 5 barrier — so neither
   can let an order out. Placing the push here rather than after the barrier also means the barrier's
   own `clearinghouseState` read observes an already-aligned venue, so the first reconcile cycle
   cannot manufacture a spurious divergence.**)**
   **(Extended again by ADR-0046 §3:** on **live** this step now *opens* with an unsigned
   `userAbstraction` read gating everything above — the account's abstraction mode must be
   Manual/Standard (an allowlist of `default` / `disabled`), and anything else raises
   **`VenueAccountModeUnsupported`**, a third `InvariantViolation` beside `StoreAccountMismatch` and
   `VenueLeverageMismatch`. It is ordered **before** the leverage push and nothing else touches the
   venue until it passes: a pooled mode makes `clearinghouseState` a perps *sub-ledger* rather than
   the account, so every margin number the push and the barrier reason from would be off by an order
   of magnitude, and reporting a leverage mismatch computed against it would be noise on top of an
   error. It is a no-op on **paper** — the mode is a live-only venue concept.**)**
5. **Startup-reconciliation barrier** — the ADR-0011 mass-rebuild. A **hard gate**: nothing places
   until it succeeds. **(Extended by ADR-0043 §6:** on **live** the barrier also performs a single
   unsigned `clearinghouseState` read to **materialise the account row** when the ledger has none —
   otherwise a live first run would start strategies with no account row at all, against ADR-0041
   §6's promise that `cash` is Tier-1 and never `None`. It is a no-op on **paper**: the row already
   exists by the time the barrier runs, seeded three steps earlier by step 2's check — paper's
   opening value is declared config, so that write needs no venue read and no gate. A *full* ledger
   reconcile inside the barrier was rejected; positions, Tier-1 heals and the ADR-0040 §6
   divergence alerts stay on the cadence.**)** **(Sharpened by
   [#191](https://github.com/MarcosACH/tickwright/issues/191), which landed it:** the
   materialisation is ordered **before** the mass-rebuild, not merely inside the same step. The
   rebuild emits synthetic fills; every fill's write carries the account row (ADR-0043 §9 — every
   mutation moves cash), so a rebuild that ran first would *create* the live row itself at the zero
   `Account.open` resolves a `None` genesis to, and the materialisation behind it would decline to
   overwrite a row that now exists — leaving that zero standing for the life of the ledger with
   nothing to refuse it, since #188's genesis comparison is paper-only. The live row must be
   materialised, never fallen into. The gate is therefore an **ordered** sequence of proofs rather
   than one call, and the runner composes it: a `StartupBarrier` holds the shared retry policy
   below, the step order lives in the runner's own sequence, and an attempt stops at the first step
   that froze.**)**
6. Start the `Strategy` instances (`on_start`: restore snapshot; seq high-water from the saga
   store, ADR-0016; then **pull current open-order state from the `Cache` read-model** by direct
   method call, ADR-0004 — the barrier's reconciliation `OrderEvent`s (step 5) were published
   before strategies subscribed and are gone on `InMemoryBus`, so the contract is
   **pull-then-subscribe: never rely on having seen startup events** — documented in
   `extending.md`).
7. Start the `MarketFeed` **last** — the first tick is only possible after the barrier clears, so
   no order can be placed before reconciliation completes (ADR-0011 inv 5).

**Barrier-failure policy: bounded retry, then fail-fast.** The barrier retries the mass-rebuild
with backoff up to `startup_reconciliation_timeout`. A transient boot-time venue blip resolves and
we proceed; a sustained outage trips the timeout → `FAULTED` → the process exits non-zero → the
external supervisor backoff-restarts (re-entering recovery). This composes ADR-0011's
freeze-don't-guess with ADR-0014's crash-only, avoiding both a hung half-alive engine and a tight
crash-loop. (Live-path only; the barrier cannot fail on the paper exchange.)
**(Extended by ADR-0043 §6:** this policy covers the account-materialisation read as well as the
mass-rebuild — same backoff, same `startup_reconciliation_timeout` budget, same `FAULTED` exit when
it expires — rather than the read getting a policy of its own. Clearing the barrier with no account
row is **not** an available outcome: that is the state the step exists to prevent, so a
`clearinghouseState` that will not answer must fault the process, never be read as an empty
ledger — the freeze-don't-guess rule above, applied to the account line.**)**
**(Extended again by ADR-0044 §6:** step 4's leverage push runs under this same policy and the same
`startup_reconciliation_timeout` budget — bounded retry with backoff, then `FAULTED` — rather than
minting a second timeout, for the same reason ADR-0043 gave: it is boot-window venue I/O facing the
same transient-blip reality. A venue **no-change** error counts as success, a rate-limit rejection is
retried, anything else consumes the budget and faults. Clearing startup with a venue we failed to
align is not an available outcome. Note this is *retry* policy only — a **mismatch** on a held
position is not retryable and refuses immediately (§5 there).**)**
**(Extended a third time by ADR-0046 §3:** step 4's abstraction-mode gate joins the same policy —
bounded retry with backoff inside the same `startup_reconciliation_timeout` budget, then `FAULTED`,
for the same reason the two above give. **"Assume standard on error" is not an available outcome**:
an unreadable mode is the freeze-don't-guess rule applied to a precondition, and clearing startup
without knowing what `clearinghouseState` *means* is exactly the state the gate exists to prevent.
As with ADR-0044, this is *retry* policy only — an **unsupported** mode is not retryable and refuses
immediately.**)**

**Who enforces the budget at step 4.** The three extensions above put step 4's venue work under
`startup_reconciliation_timeout` rather than minting a second timeout, and that is the decision — but
the budget is enforced by the **adapter**, not the runner. The runner applies its timeout to the
barrier at step 5; nothing wraps the `Exchange.start()` call at step 4, and `EngineConfig` does not
reach the adapter, so an adapter cannot read the number it is told to retry inside. Two consequences
the slice that first makes step 4 a real network call must handle: `start()` **must not hang** (a
boot wedged there is unbounded, and the task watching SIGINT is not created until the start sequence
returns, so SIGKILL is the only escape — the opposite of the bounded teardown below), and the
composition root must **hand the adapter the budget** to size its own timeouts with. Bounding the
start sequence in the runner is the alternative and was not taken here: `bus.start()` at step 3 has
the same exposure, so the fix belongs to the whole sequence rather than to this one call.

## Shutdown reverses the sequence and leaves resting orders alone

`Engine.stop()`, bounded by `shutdown_timeout` so teardown cannot hang: stop the feed (cut the
source) → let the drain-to-quiescence FIFO (ADR-0023) go idle, with a bounded post-stop delay for
trailing `ExecutionReport`s → stop strategies (`on_stop` takes a **final `snapshot()`**, ADR-0016)
→ stop the `Exchange`/`ExecutionManager` (disconnect; Kafka flush/commit) → stop the bus, close the
store → exit **0**.

**(Extended by [#186](https://github.com/MarcosACH/tickwright/issues/186):** the `Exchange` stop is
**not** where the sentence above puts it. `exchange.stop` sits in `_teardown_steps` behind
`feed.stop` (ADR-0044 §7) and behind `reconcile.stop`, ahead of the drain — not after the strategies
stop. Both ends of that slot are load-bearing, and they pull in opposite directions. What the slot
ends is a loop the venue runs — ADR-0037's paper funding generator, since
[#226](https://github.com/MarcosACH/tickwright/issues/226) supervised as `Exchange.run()` and so
cancelled by the runner rather than owned by the adapter — and it must precede the **drain**: a loop
still alive during the drain keeps publishing into it and keeps raising
its high-water mark, so the cascade the drain is waiting on never quiesces and the bound is spent on
a shutdown that is generating its own work. But the reconcile cadences **read** the adapter
(`fetch_order`), and they run until `reconcile.stop` cancels them, so releasing the venue ahead of
that would leave a live cycle querying an adapter this very sequence had just torn down — a
self-inflicted freeze (ADR-0011 inv 1) in the one window where nothing can act on it. Silence the
readers, then release what they were reading. Everything else in the sentence stands: the drain still
precedes the final strategy snapshots (ADR-0016), and the bus and the store still close last.

Ahead of the drain does **not** mean after the last caller, and the ordering cannot make it so. The
drain dispatches the cascade it waits on, and `host.stop` — one step behind it — only snapshots; it
never unsubscribes. So an in-flight tick can still reach a strategy after the venue is released, and
its `Signal` still reaches the `ExecutionManager` and its `place`. On the in-memory bus this is
unreachable (`publish` drained the cascade before the feed was cut); on Kafka, where dispatch runs in
the poll loop, it is not. Moving the release behind the drain to close it would re-open the loop
window above, which is the worse of the two — so this one is answered at the **seam** instead:
`Exchange.stop()` must leave `place`/`cancel` answerable, refusing cleanly rather than hanging, until
the drain behind it is done. A step order cannot express "released, but still answering"; a contract
can.

The membership is **one ordered tuple** walked by both teardown paths, so the graceful and faulted
paths cannot disagree about it — they differ in failure *policy* only. That one-membership rule has a
cost every seam in it must carry, not just the venue: the faulted pass re-walks **from the top**, so a
graceful step that raises drives every step ahead of the break a second time. No entry may treat a
second call as an error. The five that cross a seam are each specified idempotent there —
`MarketFeed.stop()`, `Exchange.stop()`, `EventBus.drain()`, `EventBus.close()` and `Store.close()`;
the remaining two are the engine's own (`_stop_cadences`, `StrategyHost.stop`) and answer for it at
the membership. **Two entries are both**: `feed.stop` and — since
[#226](https://github.com/MarcosACH/tickwright/issues/226) — `exchange.stop` each wrap a seam call
*and* the cancellation of a task this runner supervises, so each inherits the property from both
halves, and neither costs the membership an entry. An adapter's supervised half is not a separate
teardown seam; it is the same seam's other end.**)**

- `SUBMITTED` orders in flight on the wire are **not** awaited — they stay `SUBMITTED`,
  checkpointed; restart reconciliation heals them (ADR-0008 residual risk).
- A graceful stop **does not cancel resting `LIVE` orders.** Snapshot-plus-reconcile re-adopts them
  by `cloid` on restart, so crash and graceful stop **converge on one recovery path**. Cancelling on
  stop would fork the two paths and push failure-prone network calls into teardown. Flatten-on-exit
  is a risk/operator policy, not engine mechanics (deferred to the strategy/kill-switch surface).
  **(Consumed by ADR-0044 §4:** this is what makes an own resting order the *ordinary* actor in the
  step-4 leverage read→write race — the order is live on the venue for the whole of the next boot's
  step 4 and can fill mid-window, so "the feed starts last" bounds new **sends**, never **fills**.
  A future flatten-on-exit or cancel-on-stop policy would shrink that race; nothing else depends on
  it, and ADR-0044 accepts the race regardless.**)**

## Supervision is `asyncio.TaskGroup`; two error classes map to two mechanisms

`asyncio.run(engine.run())`; `run()` performs the startup above, then supervises the long-running
component tasks (feed read loop, reconciliation loops, Kafka drains) under an **`asyncio.TaskGroup`**
(Python 3.13, ADR-0021). The first task to raise an **invariant violation** causes the TaskGroup to
cancel its siblings; best-effort stop hooks run (each failure recorded as
`engine.stop_hook_failed`, never swallowed silently); the engine goes `FAULTED`; the process exits
**non-zero**. A hand-rolled `gather` + supervisor was rejected — it re-implements exactly this with
room for a missed-propagation bug.

> **Shipped state (as of the #49 runner).** The TaskGroup supervises the feed read loop, the
> barrier-gated startup, and the **continuous reconciliation loops**: `reconcile_inflight` and
> `reconcile_open_orders` run as `run_cadence` tasks paced by their `ReconcileConfig` intervals
> off `Clock.sleep_until` — the virtual-time waiter that keeps replay deterministic (ADR-0033).
> The reverse shutdown cancels them right after the feed stops, before the bus drains. Kafka
> drains land with the Kafka bus (#20).
>
> **Extended by [#226](https://github.com/MarcosACH/tickwright/issues/226): the venue's own
> long-lived half joins them, as `Exchange.run()`.** The containment rule below is stated in terms
> of *handler origin*, and that is only half of what decides it: a raw handler's exception reaches
> this TaskGroup because the **publisher** is a supervised task. ADR-0037's paper funding generator
> was the one publisher that was not — `PaperExchange.start()` spawned it with a bare `create_task`
> — so a ledger write the store refused killed it alone, leaving the engine `RUNNING`, accruing
> nothing for the life of the process, and exiting on whatever the operator eventually asked for.
> Enrolling it needed a seam member, because step 4's `Exchange.start()` is awaited inline and must
> return so the barrier can run at step 5; `run()` is that member, task-created here beside the feed
> (ADR-0044 §7). An adapter with no loop — `HyperliquidExchange` — returns immediately and its task
> completes, which nothing downstream distinguishes from a loop that is merely idle.
>
> **One consequence, accepted:** the generator's resume instant moves from step 4 to TaskGroup-open,
> i.e. **behind the barrier**. Under a virtual clock this is unobservable. Under a live one,
> boundaries that elapse *during* the barrier are skipped rather than settled — the same guarantee
> the feed has, and arguably the more correct reading, since nothing may act on a venue this engine
> has not finished aligning.

- **OS signals.** `SIGINT`/`SIGTERM` (via `loop.add_signal_handler`) set a stop event → the graceful
  shutdown above → exit **0**. `SIGKILL` is uncatchable → crash-only recovery on next boot,
  deliberately indistinguishable from any other crash.
- **Exit-code contract** (what the external supervisor keys on): **0 = graceful, non-zero =
  `FAULTED` → restart.**

**The two ADR-0014 error classes are drawn by handler origin, not by exception type.** The Engine
wraps **third-party** handlers (`Strategy.on_tick`/`on_order_event`, `MarketFeed` parse callbacks) in
a containment adapter when it subscribes them: catch `Exception` (except `InvariantViolation` and
`BaseException`), log with the correlation id, emit a named event (`strategy.error`/`handler.error`),
and **continue** — one bad tick or a third-party strategy bug must not fault the engine.
**Engine-internal** handlers (`ExecutionManager` saga, reconciler, store writes) are subscribed
**raw**: any exception propagates to the TaskGroup and faults. An explicit `InvariantViolation` type
(illegal saga transition, failed checkpoint, broken guard contract) **pierces even the containment
net** and always faults. The bus stays a pure transport — containment is a property of *how the
Engine subscribes* third-party components. A type-based-only scheme (run everything raw, catch only a
designated `RecoverableError`) was rejected: it inverts the default for untrusted strategy code, so an
unexpected `KeyError` in a third-party strategy would fault the whole engine.
