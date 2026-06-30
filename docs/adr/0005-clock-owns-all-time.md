# A Clock Protocol owns all time: reads, waits, and timers

Time is injected through a `Clock` Protocol that owns **every** interaction with time —
reads (`now()` / `timestamp_ns()`), waiting (`sleep(secs)`), and timer scheduling
(`call_at` / `set_timer`). Engine code never calls `asyncio.sleep` or `time.time()` directly;
that is a banned pattern enforced at review/lint. Two impls per the seam rule: `LiveClock`
(wall clock + real async waits + real-loop timers) and `ManualClock` (virtual time advanced
explicitly by tests; `sleep()` returns immediately; timers fire deterministically when virtual
time crosses them).

A Clock that owned only time *reads* would leave reconciliation's grace loop, retry backoff,
and the paper exchange's latency sim calling `asyncio.sleep` directly — so determinism would
leak exactly where timing is load-bearing (the "retry budget capped below the ghost grace
window" invariant). Owning waits and timers too lets a test drive a 120s reconcile loop in
microseconds with ordering pinned, using a TestClock-advances-virtual-time
model.

Canonical event timestamp is **UTC epoch nanoseconds (`int`)** —
converts cleanly to Hyperliquid's milliseconds, no timezone ambiguity; datetime conversion
happens only at human-facing edges (logs).
