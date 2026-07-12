"""``Backoff`` — the reconnect pacing policy, tested without a feed.

The doubling-with-cap arithmetic and the reset-on-success rule are one invariant
the feed's reconnect loop stands on; here they are exercised directly against a
recording ``Clock``, so no feed (and no real time) is involved.
"""

import asyncio

from tickwright.adapters.clock import ManualClock
from tickwright.venues.hyperliquid.backoff import Backoff


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
