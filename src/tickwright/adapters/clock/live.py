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
