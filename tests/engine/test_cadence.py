"""The virtual-time cadence loop (ADR-0033): paces a periodic cycle off the
``Clock`` seam via ``sleep_until``, so under ``ManualClock`` it fires only when
the feed drives virtual time across the deadline — never busy-spinning, never
moving time backward — and reschedules from *now* (a large replay time-jump
fires the cycle once, not once per missed interval).
"""

import asyncio

from tickwright.adapters.clock import ManualClock
from tickwright.engine.cadence import run_cadence

_NS = 1_000_000_000


def test_cadence_fires_each_time_the_feed_drives_time_past_the_interval() -> None:
    async def main() -> list[int]:
        clock = ManualClock(start_ns=0)
        fired: list[int] = []

        async def cycle() -> bool:
            fired.append(clock.timestamp_ns())
            return True

        task = asyncio.create_task(run_cadence(clock=clock, interval_seconds=5.0, cycle=cycle))
        await asyncio.sleep(0)  # park on the first deadline (t=5s)
        assert fired == []
        clock.advance_to(3 * _NS)  # short of the deadline: nothing fires
        await asyncio.sleep(0)
        assert fired == []
        clock.advance_to(6 * _NS)  # crossing t=5s fires exactly once
        for _ in range(3):
            await asyncio.sleep(0)
        assert fired == [6 * _NS]
        clock.advance_to(11 * _NS)  # next deadline rescheduled from t=6s → t=11s
        for _ in range(3):
            await asyncio.sleep(0)
        task.cancel()
        return fired

    assert asyncio.run(main()) == [6 * _NS, 11 * _NS]


def test_a_large_time_jump_fires_the_cycle_once_not_once_per_missed_interval() -> None:
    async def main() -> list[int]:
        clock = ManualClock(start_ns=0)
        fired: list[int] = []

        async def cycle() -> bool:
            fired.append(clock.timestamp_ns())
            return True

        task = asyncio.create_task(run_cadence(clock=clock, interval_seconds=5.0, cycle=cycle))
        await asyncio.sleep(0)
        clock.advance_to(60 * _NS)  # 12 intervals at once
        for _ in range(5):
            await asyncio.sleep(0)
        task.cancel()
        return fired

    assert asyncio.run(main()) == [60 * _NS]
