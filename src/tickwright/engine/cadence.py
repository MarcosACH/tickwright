"""``run_cadence`` — the virtual-time periodic loop (ADR-0033).

Paces a cycle off ``Clock.sleep_until``, a pure waiter: under ``ManualClock``
each deadline fires only when the feed drives virtual time across it, so the
loop never busy-spins and never moves time backward; under ``LiveClock`` it is
an ordinary wall-clock timer. Deadlines reschedule from *now* after each run —
a large replay time-jump fires the cycle once, not once per missed interval.
The loop runs until cancelled; the runner supervises it in the ``TaskGroup``
and cancels it in the reverse shutdown (ADR-0024). A cycle reporting failure
(``False`` — a frozen reconcile pass) is simply retried at the next deadline.
"""

from collections.abc import Awaitable, Callable

from tickwright.domain import Clock

_NS_PER_SECOND = 1_000_000_000


async def run_cadence(
    *, clock: Clock, interval_seconds: float, cycle: Callable[[], Awaitable[bool]]
) -> None:
    """Run ``cycle`` every ``interval_seconds`` of clock time, forever."""
    interval_ns = int(interval_seconds * _NS_PER_SECOND)
    while True:
        await clock.sleep_until(clock.timestamp_ns() + interval_ns)
        await cycle()
