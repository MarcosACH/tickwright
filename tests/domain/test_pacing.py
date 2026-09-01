"""``Backoff`` and ``Deadline`` — the retry pacing rules, tested without a caller.

Every bounded retry in the system stands on these two: the doubling-with-cap
arithmetic and the reset-on-success rule on one side, and "opened once, spent
once, asked only after a failure" on the other. Exercised here directly against
a recording ``Clock``, so neither a feed, nor a barrier, nor a venue (nor any
real time) is involved — which is the point of them being ``domain`` values
rather than a rule three loops each wrote for themselves.
"""

import asyncio

from tickwright.adapters.clock import ManualClock
from tickwright.domain import Backoff, Deadline


class RecordingClock(ManualClock):
    """A ``ManualClock`` that records what it was asked to sleep."""

    def __init__(self) -> None:
        super().__init__()
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        await super().sleep(seconds)


def test_sleep_on_doubles_the_delay_each_call() -> None:
    async def main() -> RecordingClock:
        clock = RecordingClock()
        backoff = Backoff(initial=1.0, maximum=60.0)
        await backoff.sleep_on(clock)
        await backoff.sleep_on(clock)
        await backoff.sleep_on(clock)
        return clock

    clock = asyncio.run(main())
    assert clock.sleeps == [1.0, 2.0, 4.0]


def test_sleep_on_caps_the_delay_at_maximum() -> None:
    async def main() -> RecordingClock:
        clock = RecordingClock()
        backoff = Backoff(initial=1.0, maximum=4.0)
        for _ in range(5):
            await backoff.sleep_on(clock)
        return clock

    clock = asyncio.run(main())
    assert clock.sleeps == [1.0, 2.0, 4.0, 4.0, 4.0]  # never exceeds the cap


def test_reset_returns_to_the_initial_delay() -> None:
    async def main() -> RecordingClock:
        clock = RecordingClock()
        backoff = Backoff(initial=1.0, maximum=60.0)
        await backoff.sleep_on(clock)  # 1.0
        await backoff.sleep_on(clock)  # 2.0
        backoff.reset()
        await backoff.sleep_on(clock)  # back to 1.0
        return clock

    clock = asyncio.run(main())
    assert clock.sleeps == [1.0, 2.0, 1.0]


def test_a_deadline_is_not_spent_before_its_budget_elapses() -> None:
    clock = ManualClock()
    deadline = Deadline.opening(clock=clock, budget_seconds=60.0)
    assert not deadline.spent(clock)


def test_a_deadline_is_spent_the_instant_the_budget_is_reached() -> None:
    """Reached, not passed: ``>=``, so a budget spent exactly to the nanosecond
    refuses rather than granting one more attempt off a rounding edge."""

    async def main() -> tuple[Deadline, ManualClock]:
        clock = ManualClock()
        deadline = Deadline.opening(clock=clock, budget_seconds=60.0)
        await clock.sleep(60.0)
        return deadline, clock

    deadline, clock = asyncio.run(main())
    assert deadline.spent(clock)


def test_a_deadline_carries_the_budget_it_was_opened_with() -> None:
    """The refusals print the window that was spent, and an absolute nanosecond
    instant cannot say what it was — so the budget rides along on the value."""
    deadline = Deadline.opening(clock=ManualClock(), budget_seconds=42.5)
    assert deadline.budget_seconds == 42.5


def test_two_guards_sharing_one_deadline_share_one_window() -> None:
    """The decision the type exists for (ADR-0044 §6): what the first spends is
    gone from what the second has left. A budget passed twice would give each a
    fresh 60s and take 120s to refuse; one opened deadline refuses at 60s."""

    async def main() -> tuple[Deadline, ManualClock]:
        clock = ManualClock()
        deadline = Deadline.opening(clock=clock, budget_seconds=60.0)
        await clock.sleep(45.0)  # the first guard's retries
        assert not deadline.spent(clock)
        await clock.sleep(20.0)  # the second guard's, past the shared window
        return deadline, clock

    deadline, clock = asyncio.run(main())
    assert deadline.spent(clock)
