"""``LiveClock`` — the wall-clock ``Clock`` for live operation (ADR-0005).

Engine code never calls ``time.time()`` or ``asyncio.sleep`` directly; it flows
through this adapter, so swapping in ``ManualClock`` makes every timing path
deterministic in tests with no code change.
"""

import asyncio
import time
from datetime import UTC, datetime


class LiveClock:
    """Reads the system clock and waits on the real event loop."""

    def timestamp_ns(self) -> int:
        return time.time_ns()

    def now(self) -> datetime:
        return datetime.now(tz=UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def sleep_until(self, ts_ns: int) -> None:
        """Wait until the wall clock crosses ``ts_ns`` (ADR-0033). Loops because
        ``asyncio.sleep`` may wake a hair early relative to ``time.time_ns``."""
        while (remaining_ns := ts_ns - self.timestamp_ns()) > 0:
            await asyncio.sleep(remaining_ns / 1_000_000_000)
