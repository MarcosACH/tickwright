"""``ManualClock`` — virtual time advanced explicitly, so tests never sleep.

Canonical time is UTC epoch nanoseconds (ADR-0005). ``ReplayFeed`` advances this
clock to each tick's ``ts_event`` before publishing, which is what makes replay
deterministic *in time*: reconcile loops, retry backoff, and paper-latency timers
all fire relative to the replayed stream rather than the wall clock (ADR-0027).
"""

from datetime import UTC, datetime

_NS_PER_SECOND = 1_000_000_000


class ManualClock:
    """A ``ReplayClock``: reads virtual time and lets the feed advance it."""

    def __init__(self, start_ns: int = 0) -> None:
        self._now_ns = start_ns

    def timestamp_ns(self) -> int:
        return self._now_ns

    def now(self) -> datetime:
        return datetime.fromtimestamp(self._now_ns / _NS_PER_SECOND, tz=UTC)

    async def sleep(self, seconds: float) -> None:
        # Virtual: returns immediately, advancing time as if the wait elapsed.
        self._now_ns += int(seconds * _NS_PER_SECOND)

    def advance_to(self, ts_ns: int) -> None:
        """Advance virtual time to ``ts_ns``. Time never moves backward."""
        if ts_ns < self._now_ns:
            raise ValueError(f"cannot move ManualClock backward: {ts_ns} < current {self._now_ns}")
        self._now_ns = ts_ns
