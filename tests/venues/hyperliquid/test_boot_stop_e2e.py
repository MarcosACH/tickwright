"""A stop during a venue outage at boot ends the engine soon (issue #614).

Docker sends SIGKILL 30 s after SIGTERM. A boot can wait on the venue for a whole
startup budget per step, 60 s by default. So the stop has to end the boot.

The real ``Engine`` against the real ``HyperliquidExchange``, with only the HTTP
transport and the WebSocket fake. The outage is a venue that never answers one
kind of request, as a black-holed route does.
"""

import asyncio
from decimal import Decimal
from pathlib import Path

from hyperliquid_fakes import TEST_SIGNING_KEY, FakeExchangeApi, VenueDarkOn, idle_ws
from pydantic import SecretStr

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import ComponentState, InstrumentSpec
from tickwright.engine.runner import Engine
from tickwright.observability.testing import capture_events
from tickwright.venues.hyperliquid import (
    HyperliquidConfig,
    HyperliquidExchange,
    HyperliquidFeed,
    HyperliquidUniverse,
)

BTC_SPEC = InstrumentSpec(
    symbol="BTC", sz_decimals=5, max_decimals=6, min_notional=Decimal("10"), max_sig_figs=5
)
START_MS = 1_700_000_000_000
_SUMMARY = {
    "accountValue": "100.0",
    "totalMarginUsed": "0.0",
    "totalNtlPos": "0.0",
    "totalRawUsd": "100.0",
}
FLAT_ACCOUNT = {
    "assetPositions": [],
    "crossMaintenanceMarginUsed": "0.0",
    "crossMarginSummary": _SUMMARY,
    "marginSummary": _SUMMARY,
    "time": START_MS,
    "withdrawable": "100.0",
}


def _dark_on(dark: str) -> VenueDarkOn:
    """A flat account with no orders, dark on one kind of request."""
    return VenueDarkOn(
        dark,
        FakeExchangeApi(
            {
                "userAbstraction": "disabled",
                "clearinghouseState": FLAT_ACCOUNT,
                "frontendOpenOrders": [],
                "userFills": [],
                "userFillsByTime": [],
            }
        ),
    )


def _stop_during_outage(tmp_path: Path, venue: VenueDarkOn) -> tuple[int, Engine]:
    async def life() -> tuple[int, Engine]:
        bus = InMemoryBus()
        clock = ManualClock(start_ns=START_MS * 1_000_000)
        config = HyperliquidConfig(
            testnet=True, symbols=["BTC"], signing_key=SecretStr(TEST_SIGNING_KEY)
        )
        engine = Engine(
            bus=bus,
            clock=clock,
            store=SQLiteStore(tmp_path / "saga.db"),
            exchange=HyperliquidExchange(
                config=config,
                bus=bus,
                clock=clock,
                universe=HyperliquidUniverse(specs={"BTC": BTC_SPEC}, asset_indices={"BTC": 3}),
                post=venue,
                connect=idle_ws,
                startup_timeout_seconds=60.0,
                cut_part_wait_seconds=30.0,
            ),
            feed=HyperliquidFeed(config=config, bus=bus, clock=clock, connect=idle_ws),
        )
        # Far below Docker's 30 s grace period. A boot that waits for the venue
        # never returns, so this bound is what turns that into a failure.
        return await asyncio.wait_for(engine.run(), timeout=2), engine

    return asyncio.run(life())


def test_sigterm_while_the_exchange_starts_against_a_dark_venue_ends_the_run(
    tmp_path: Path,
) -> None:
    # The mode read is the first thing ``Exchange.start`` asks the venue.
    venue = _dark_on("userAbstraction")

    with capture_events() as logs:
        exit_code, engine = _stop_during_outage(tmp_path, venue)

    assert venue.stopped
    assert exit_code == 0
    assert engine.state is ComponentState.STOPPED
    # Nothing reaches the venue after the operator asked to stop.
    assert venue.after_stop == []
    events = [record["event"] for record in logs]
    assert "engine.barrier_cleared" not in events
    assert events[-1] == "engine.stopped"


def test_sigterm_while_the_barrier_reads_the_account_from_a_dark_venue_ends_the_run(
    tmp_path: Path,
) -> None:
    # With no leverage to push, ``Exchange.start`` reads only the mode. So the
    # one account read is the barrier's, which opens the account row.
    venue = _dark_on("clearinghouseState")

    with capture_events() as logs:
        exit_code, engine = _stop_during_outage(tmp_path, venue)

    assert venue.stopped
    assert exit_code == 0
    assert engine.state is ComponentState.STOPPED
    assert venue.after_stop == []
    # The barrier never opened the account row, so the next boot reads it again.
    assert SQLiteStore(tmp_path / "saga.db").load_account() is None
    events = [record["event"] for record in logs]
    assert "engine.barrier_cleared" not in events
    assert events[-1] == "engine.stopped"
