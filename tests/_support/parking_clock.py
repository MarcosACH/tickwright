"""A manual clock whose ``sleep`` stays open, for a case that acts during a wait (#682)."""

import asyncio

from tickwright.adapters.clock import ManualClock


class ParkingClock(ManualClock):
    """A clock whose ``sleep`` waits like a real one.

    ``ManualClock.sleep`` returns at once, as if the time had passed with
    nothing in it. That is the right answer for "no mark arrived in time". A
    case that delivers a mark *during* the wait needs the wait to stay open
    until the case decides what happens in it. ``advance_to`` ends the wait.

    ``parked`` tells the case a ``sleep`` has started. The clock never clears
    it, so a case that waits for a later ``sleep`` clears it first.
    """

    def __init__(self, start_ns: int = 0) -> None:
        super().__init__(start_ns)
        self.parked = asyncio.Event()

    async def sleep(self, seconds: float) -> None:
        self.parked.set()
        await self.sleep_until(self.timestamp_ns() + int(seconds * 1_000_000_000))
