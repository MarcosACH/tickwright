"""Clock adapters (ADR-0005). The tracer runs entirely on ``ManualClock`` so the
suite never sleeps and every timestamp is deterministic. ``LiveClock`` is the
wall-clock second impl; only its read surface is smoke-checked here.
"""

import asyncio
from datetime import UTC, datetime

import pytest

from tickwright.adapters.clock import LiveClock, ManualClock


def test_manual_clock_defaults_to_epoch_zero() -> None:
    assert ManualClock().timestamp_ns() == 0


def test_manual_clock_starts_at_a_given_time() -> None:
    assert (
        ManualClock(start_ns=1_700_000_000_000_000_000).timestamp_ns() == 1_700_000_000_000_000_000
    )


def test_advance_to_moves_virtual_time_forward() -> None:
    clock = ManualClock()
    clock.advance_to(5_000)
    assert clock.timestamp_ns() == 5_000


def test_advance_to_the_current_instant_is_idempotent() -> None:
    clock = ManualClock(start_ns=42)
    clock.advance_to(42)
    assert clock.timestamp_ns() == 42


def test_advance_to_the_past_is_rejected() -> None:
    clock = ManualClock(start_ns=100)
    with pytest.raises(ValueError):
        clock.advance_to(99)


def test_now_is_a_utc_datetime_matching_timestamp_ns() -> None:
    clock = ManualClock(start_ns=1_000_000_000)  # 1 second past the epoch
    now = clock.now()
    assert now.tzinfo is UTC
    assert now == datetime(1970, 1, 1, 0, 0, 1, tzinfo=UTC)


def test_manual_sleep_returns_immediately_and_advances_virtual_time() -> None:
    clock = ManualClock(start_ns=1_000)
    asyncio.run(clock.sleep(2))  # 2 seconds; no wall-clock wait
    assert clock.timestamp_ns() == 1_000 + 2_000_000_000


def test_live_clock_reads_are_monotonic_and_utc() -> None:
    clock = LiveClock()
    assert clock.now().tzinfo is UTC
    assert clock.timestamp_ns() > 0


def test_manual_sleep_until_parks_until_the_feed_advances_past_the_target() -> None:
    """The virtual-time cadence primitive (ADR-0033): ``sleep_until`` is a pure
    waiter — it never advances time itself, and only returns once a producer
    (``advance_to``) drives virtual time across the target instant."""

    async def main() -> list[str]:
        clock = ManualClock(start_ns=1_000)
        trail: list[str] = []

        async def sleeper() -> None:
            await clock.sleep_until(5_000)
            trail.append("woke")

        task = asyncio.create_task(sleeper())
        await asyncio.sleep(0)  # let the sleeper park
        assert not task.done()
        clock.advance_to(3_000)  # short of the target: still parked
        await asyncio.sleep(0)
        assert not task.done()
        trail.append("advancing past")
        clock.advance_to(5_000)  # crossing the target releases the waiter
        await task
        return trail

    assert asyncio.run(main()) == ["advancing past", "woke"]


def test_manual_sleep_until_a_past_or_present_target_returns_immediately() -> None:
    async def main() -> None:
        clock = ManualClock(start_ns=5_000)
        await clock.sleep_until(3_000)  # already behind us
        await clock.sleep_until(5_000)  # the current instant counts as crossed
        assert clock.timestamp_ns() == 5_000  # and neither wait moved time

    asyncio.run(main())


def test_manual_sleep_until_survives_a_cancelled_sleeper() -> None:
    """Shutdown cancels the cadence tasks mid-park; the stale registration must
    not wedge or break later ``advance_to`` calls."""

    async def main() -> None:
        clock = ManualClock(start_ns=1_000)
        task = asyncio.create_task(clock.sleep_until(5_000))
        await asyncio.sleep(0)  # park it
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        clock.advance_to(9_000)  # crossing the dead waiter's target is harmless
        assert clock.timestamp_ns() == 9_000

    asyncio.run(main())


def test_live_sleep_until_waits_out_the_wall_clock_delta() -> None:
    async def main() -> None:
        clock = LiveClock()
        target_ns = clock.timestamp_ns() + 20_000_000  # 20ms out
        await clock.sleep_until(target_ns)
        assert clock.timestamp_ns() >= target_ns
        # A target already behind us returns without waiting.
        await clock.sleep_until(clock.timestamp_ns() - 1)

    asyncio.run(main())
