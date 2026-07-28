# Periodic loops pace off `Clock.sleep_until`, a pure waiter virtual time drives

The continuous reconciliation cadences (ADR-0011) need a periodic loop inside the runner's
`TaskGroup` (ADR-0024) that is correct under both clocks (ADR-0005): wall time in live operation,
feed-driven virtual time in replay and tests (ADR-0027). The naive loop —
`while running: await clock.sleep(interval); await cycle()` — is wrong under `ManualClock`:
`sleep` *advances* virtual time immediately and never suspends, so the loop busy-spins and races
`ReplayFeed.advance_to` into a backward-time `ValueError`. Pacing off `asyncio.sleep` instead
would violate ADR-0005 and make the deterministic-replay promise false.

The primitive is one method on the `Clock` seam — `sleep_until(ts_ns)` — defined as a **pure
waiter**: it never advances time itself, and returns only once the clock has actually crossed the
target instant. `LiveClock` waits out the wall-clock delta (looping, since `asyncio.sleep` can
wake a hair early). `ManualClock` parks an `asyncio.Future` per waiter and `advance_to` resolves
every waiter whose target the advance crossed — so a cadence built on it fires exactly when the
feed drives virtual time past the deadline, never moves time backward, and never spins. A
cancelled waiter (shutdown) is dropped on the next advance; it cannot wedge the clock.

On top of it sits one loop, `engine/cadence.py::run_cadence`:
`while True: await clock.sleep_until(now + interval); await cycle()`. Deadlines reschedule **from
now, with no catch-up**: a large replay time-jump (sparse ticks) fires the cycle once, not once
per missed interval — the cycles are convergent state checks (re-running heals nothing new), so
replaying missed firings would be pure noise. The loop ignores the cycle's result: a failed pass
(a frozen reconcile cycle — ADR-0011 inv 1) is retried at the next deadline, not tighter — freeze
semantics already guarantee nothing was guessed. The loop runs until cancelled; the runner supervises both
cadence tasks in its `TaskGroup` and cancels them in the reverse shutdown, once the feed and the
exchange are stopped and before the bus drains (ADR-0024) — no cycle is still publishing heals into
a closing store.

One consequence lands in the feed: on the hermetic path (`ReplayFeed` + `InMemoryBus` +
`PaperExchange`) nothing ever suspends, so a waiter woken by `advance_to` would starve until
end-of-file — every deadline collapsing to one firing at EOF. `ReplayFeed` therefore yields to
the event loop once per row after advancing the clock, so a matured cadence runs *at* its crossed
deadline, before the row whose `ts_event` sits at or past it is published. Scheduling stays
deterministic: the loop's ready queue is FIFO and the yield point is fixed.

Rejected: a separate timer/`CadenceTimer` seam (a third time surface next to reads and sleeps,
against ADR-0005's "the Clock owns every interaction with time"); wall-clock pacing for the
cadences only (splits live from replay behavior exactly where timing is load-bearing); a
tick-count barrier owned by the feed (couples reconciliation pacing to tick arrival rate rather
than to the intervals `ReconcileConfig` states).
