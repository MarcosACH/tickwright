# Engine runner: ordered startup barrier, crash-only supervision, origin-based error containment

ADR-0014 fixed the *component* contract (`READY→RUNNING→STOPPED`+`FAULTED`, `start`/`stop`,
crash-only, two error classes). This ADR fixes the `Engine` **host** that wires and sequences
those components.

## Startup is an ordered sequence gated on a reconciliation barrier

`Engine.start()`:

1. Bind the `run_id` correlation, init observability, open the `Store`, build the `Cache`.
2. **Recover**: rebuild the `Cache` from the `Store` (write-through projection, ADR-0009).
3. Start the `EventBus` (InMemory: no-op; Kafka: connect consumers/producers).
4. Connect the `Exchange` + `ExecutionManager` (WS/HTTP; subscribe to `Signal`/`ExecutionReport`).
5. **Startup-reconciliation barrier** — the ADR-0011 mass-rebuild. A **hard gate**: nothing places
   until it succeeds.
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
cancel its siblings; best-effort stop hooks run; the engine goes `FAULTED`; the process exits
**non-zero**. A hand-rolled `gather` + supervisor was rejected — it re-implements exactly this with
room for a missed-propagation bug.

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
