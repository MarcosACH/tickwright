"""``run_cadence`` — the virtual-time periodic loop (ADR-0033).

Paces a cycle off ``Clock.sleep_until``, a pure waiter: under ``ManualClock``
each deadline fires only when the feed drives virtual time across it, so the
loop never busy-spins and never moves time backward; under ``LiveClock`` it is
an ordinary wall-clock timer. Deadlines reschedule from *now* after each run —
a large replay time-jump fires the cycle once, not once per missed interval.
The loop runs until cancelled; the runner supervises it in the ``TaskGroup``
and cancels it in the reverse shutdown (ADR-0024). The cycle's result is
ignored: a failed pass (e.g. a frozen reconcile cycle) is simply retried at
the next deadline.
"""

from collections.abc import Awaitable, Callable

from tickwright.domain import Clock, duration_ns


async def run_cadence(
    *, clock: Clock, interval_seconds: float, cycle: Callable[[], Awaitable[object]]
) -> None:
    """Run ``cycle`` every ``interval_seconds`` of clock time, forever."""
    interval_ns = duration_ns(interval_seconds, name="interval_seconds")
    while True:
        await clock.sleep_until(clock.timestamp_ns() + interval_ns)
        await cycle()


async def run_aligned(
    *, clock: Clock, interval_seconds: float, cycle: Callable[[int], Awaitable[object]]
) -> None:
    """Run ``cycle`` on each slot of a grid of clock time, forever (ADR-0062).

    Slots are multiples of the interval since the epoch, so they land on the
    same times in every run. ``cycle`` receives the slot in nanoseconds.
    """
    interval_ns = duration_ns(interval_seconds, name="interval_seconds")
    while True:
        slot = (clock.timestamp_ns() // interval_ns + 1) * interval_ns
        await clock.sleep_until(slot)
        await cycle(slot)
