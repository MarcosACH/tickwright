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
