"""How a bounded retry is paced, and when its budget is spent.

Two value types over the injected ``Clock``, and nothing else. Every loop in the
system that retries a failing call under a budget needs the same two answers —
*how long do I wait before trying again* and *is the window gone* — and both were
previously re-derived wherever a loop needed them: the startup barrier hand-wrote
the doubling and the nanosecond arithmetic inline, the venue package kept a
``Backoff`` the barrier could not reach, and the boot guards hand-wrote the
arithmetic a third time. The rules matched only because three docstrings said so.

They live in ``domain`` because that is the one package both ``engine`` and every
adapter may import (ADR-0032), and because the only collaborator either type has
is ``Clock``, which is already here. Sleeping on the injected clock rather than
``asyncio`` is what makes a whole retry window free under ``ManualClock`` and a
retry storm impossible against a real venue (ADR-0005: the clock owns all time).

What is deliberately **not** here is the loop itself. A retry loop has to know
which failures are worth repeating, and that is knowledge no two callers share:
the startup barrier's steps report a freeze as ``False`` (ADR-0011 inv 1 in the
return type), while the Hyperliquid boot guards catch a venue-specific tuple of
transient exceptions. Hoisting a loop over both would mean hoisting the venue's
read vocabulary into ``domain``, which ADR-0031 forbids for good reason. So the
primitives are shared and the verdict stays with the caller.
"""

from dataclasses import dataclass

from .protocols import Clock

_NS_PER_SECOND = 1_000_000_000


class Backoff:
    """A doubling delay, capped at ``maximum``, reset to ``initial`` on demand.

    The cap is the load-bearing half. An uncapped doubling carries the clock far
    past whatever deadline the loop is checking against: a large budget would
    refuse nearly a whole interval late, making real time-to-refusal up to ~2×
    the configured window. Every caller wants that bound, and none of them wants
    to remember it.

    ``reset`` exists for the loops that have something to return to — a feed's
    reconnect loop resets after a good connection (ADR-0021). A loop with one
    bounded window to clear in never calls it: there is no "good connection" to
    go back to, only a budget running out.
    """

    def __init__(self, *, initial: float, maximum: float) -> None:
        self._initial = initial
        self._maximum = maximum
        self._current = initial

    def reset(self) -> None:
        """Return to the initial delay — called after a good connection."""
        self._current = self._initial

    async def sleep_on(self, clock: Clock) -> None:
        """Sleep the current delay, then double it up to the cap for next time."""
        await clock.sleep(self._current)
        self._current = min(self._current * 2, self._maximum)


@dataclass(frozen=True)
class Deadline:
    """One instant a bounded retry must clear by, opened from a budget.

    A **value rather than a duration passed around**, because "one budget, never
    a second timeout" is a decision that only holds if the budget is opened once:
    handing the same ``timeout_seconds`` to two guards is exactly how a boot an
    operator sized at a minute comes to take two. Whatever the first spends is
    gone from what the second has left, and adding a third guard later cannot
    quietly make it three.

    ``budget_seconds`` rides along for the refusals alone. An operator reading a
    crashed boot needs the window that was spent, and an absolute nanosecond
    instant does not say what it was.
    """

    at_ns: int
    budget_seconds: float

    @classmethod
    def opening(cls, *, clock: Clock, budget_seconds: float) -> "Deadline":
        """The deadline as measured from now, on the injected clock."""
        return cls(
            at_ns=clock.timestamp_ns() + int(budget_seconds * _NS_PER_SECOND),
            budget_seconds=budget_seconds,
        )

    def spent(self, clock: Clock) -> bool:
        """Whether the budget is gone.

        Checked *after* a failure and never before an attempt, so a call that
        starts late still gets its one try — a loop that asked first would refuse
        having proved nothing, which is the one outcome a boot guard may not
        reach.
        """
        return clock.timestamp_ns() >= self.at_ns
