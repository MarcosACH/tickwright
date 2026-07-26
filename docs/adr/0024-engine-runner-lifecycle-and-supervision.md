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
   component). On **paper** it validates the configured leverage against `InstrumentSpec.max_leverage`
   and writes nothing; on **live** it validates, reads `clearinghouseState` once, and pushes
   `AppConfig.leverage` to the venue via `updateLeverage` — skipping symbols already aligned, writing
   blind where no position is held, and raising **`VenueLeverageMismatch`** where config disagrees
   with a symbol that *does* hold one. That refusal is the venue twin of step 2's
   `StoreAccountMismatch`, and the order of the two is load-bearing: the local, cheap store check
   refuses first, the networked venue check second, **both before** the step 5 barrier — so neither
   can let an order out. Placing the push here rather than after the barrier also means the barrier's
   own `clearinghouseState` read observes an already-aligned venue, so the first reconcile cycle
   cannot manufacture a spurious divergence.**)**
5. **Startup-reconciliation barrier** — the ADR-0011 mass-rebuild. A **hard gate**: nothing places
   until it succeeds. **(Extended by ADR-0043 §6:** on **live** the barrier also performs a single
   unsigned `clearinghouseState` read to **materialise the account row** when the ledger has none —
   otherwise a live first run would start strategies with no account row at all, against ADR-0041
   §6's promise that `cash` is Tier-1 and never `None`. It is a no-op on **paper**: the row already
   exists by the time the barrier runs, seeded three steps earlier by step 2's check — paper's
   opening value is declared config, so that write needs no venue read and no gate. A *full* ledger
   reconcile inside the barrier was rejected; positions, Tier-1 heals and the ADR-0040 §6
   divergence alerts stay on the cadence.**)**
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

## Shutdown reverses the sequence and leaves resting orders alone

`Engine.stop()`, bounded by `shutdown_timeout` so teardown cannot hang: stop the feed (cut the
source) → let the drain-to-quiescence FIFO (ADR-0023) go idle, with a bounded post-stop delay for
trailing `ExecutionReport`s → stop strategies (`on_stop` takes a **final `snapshot()`**, ADR-0016)
→ stop the `Exchange`/`ExecutionManager` (disconnect; Kafka flush/commit) → stop the bus, close the
store → exit **0**.

- `SUBMITTED` orders in flight on the wire are **not** awaited — they stay `SUBMITTED`,
  checkpointed; restart reconciliation heals them (ADR-0008 residual risk).
- A graceful stop **does not cancel resting `LIVE` orders.** Snapshot-plus-reconcile re-adopts them
  by `cloid` on restart, so crash and graceful stop **converge on one recovery path**. Cancelling on
  stop would fork the two paths and push failure-prone network calls into teardown. Flatten-on-exit
  is a risk/operator policy, not engine mechanics (deferred to the strategy/kill-switch surface).

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
