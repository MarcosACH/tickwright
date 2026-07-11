"""``ManualClock`` — virtual time advanced explicitly, so tests never sleep.

Canonical time is UTC epoch nanoseconds (ADR-0005). ``ReplayFeed`` advances this
clock to each tick's ``ts_event`` before publishing, which is what makes replay
deterministic *in time*: reconcile loops, retry backoff, and paper-latency timers
all fire relative to the replayed stream rather than the wall clock (ADR-0027).
"""

import asyncio
from datetime import UTC, datetime

_NS_PER_SECOND = 1_000_000_000


class ManualClock:
    """A ``ReplayClock``: reads virtual time and lets the feed advance it."""

    def __init__(self, start_ns: int = 0) -> None:
        self._now_ns = start_ns
        # Parked ``sleep_until`` waiters as (target_ns, future) pairs, released
        # when a producer drives virtual time across their target (ADR-0033).
        self._waiters: list[tuple[int, asyncio.Future[None]]] = []

    def timestamp_ns(self) -> int:
        return self._now_ns

    def now(self) -> datetime:
        return datetime.fromtimestamp(self._now_ns / _NS_PER_SECOND, tz=UTC)

    async def sleep(self, seconds: float) -> None:
        # Virtual: returns immediately, advancing time as if the wait elapsed.
        self._now_ns += int(seconds * _NS_PER_SECOND)

    async def sleep_until(self, ts_ns: int) -> None:
        """Park until virtual time crosses ``ts_ns`` — a pure waiter (ADR-0033).

        Unlike ``sleep`` this never advances time itself: a cadence loop built
        on it cannot race a replay producer backward or busy-spin; it wakes
        only when ``advance_to`` drives virtual time to or past the target.
        """
        if ts_ns <= self._now_ns:
            return
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append((ts_ns, waiter))
        await waiter

    def advance_to(self, ts_ns: int) -> None:
        """Advance virtual time to ``ts_ns``. Time never moves backward."""
        if ts_ns < self._now_ns:
            raise ValueError(f"cannot move ManualClock backward: {ts_ns} < current {self._now_ns}")
        self._now_ns = ts_ns
        self._release_matured_waiters()

    def _release_matured_waiters(self) -> None:
        still_parked: list[tuple[int, asyncio.Future[None]]] = []
        for target_ns, waiter in self._waiters:
            if waiter.done():
                continue  # cancelled sleeper: drop the stale registration
            if target_ns <= self._now_ns:
                waiter.set_result(None)
            else:
                still_parked.append((target_ns, waiter))
        self._waiters = still_parked
