"""Flatten on Hyperliquid, end to end (issue #475): the real ``Engine`` and
``Flatten`` against the real ``HyperliquidExchange`` and ``HyperliquidFeed``,
with only the HTTP transport and the WebSocket fake.

On real money the account can hold a coin the operator never configured, opened
by hand in the venue UI. Flatten must price that coin and close it.
"""

import asyncio
import json
from collections.abc import MutableMapping
from decimal import Decimal
from pathlib import Path
from typing import Any

from hyperliquid_fakes import (
    TEST_SIGNING_KEY,
    FakeExchangeApi,
    FakeWsConnection,
    asset_ctx_frame,
    fill_entry,
    filled_response,
    idle_ws,
    request_type,
    trade,
    trades_frame,
)
from pydantic import SecretStr

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import InstrumentSpec
from tickwright.engine.exit_run import Flatten
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
HYPE_SPEC = InstrumentSpec(
    symbol="HYPE", sz_decimals=2, max_decimals=4, min_notional=Decimal("10"), max_sig_figs=5
)
HYPE_ASSET = 159
CLOSE_OID = 950
START_MS = 1_700_000_000_000

_SUMMARY = {
    "accountValue": "100.0",
    "totalMarginUsed": "0.0",
    "totalNtlPos": "0.0",
    "totalRawUsd": "100.0",
}


def _account(*positions: dict) -> dict:
    """A ``clearinghouseState`` body holding ``positions``."""
    return {
        "assetPositions": list(positions),
        "crossMaintenanceMarginUsed": "0.0",
        "crossMarginSummary": _SUMMARY,
        "marginSummary": _SUMMARY,
        "time": START_MS,
        "withdrawable": "100.0",
    }


def _short(coin: str, size: str, entry: str) -> dict:
    """One cross position row. A negative ``szi`` is a short."""
    return {
        "type": "oneWay",
        "position": {
            "coin": coin,
            "szi": size,
            "entryPx": entry,
            "positionValue": "50.0",
            "unrealizedPnl": "0.0",
            "returnOnEquity": "0.0",
            "marginUsed": "10.0",
            "liquidationPx": "40.0",
            "maxLeverage": 10,
            "leverage": {"type": "cross", "value": 5},
            "cumFunding": {"allTime": "0.0", "sinceOpen": "0.0", "sinceChange": "0.0"},
        },
    }


class _Venue:
    """The account: a HYPE short opened by hand, flat once a close fills it."""

    def __init__(self) -> None:
        self.held = True

    def account(self, payload: dict) -> dict:
        return _account(_short("HYPE", "-2.0", "25.0")) if self.held else _account()

    def order(self, payload: dict) -> dict:
        self.held = False
        return filled_response(oid=CLOSE_OID, total_sz="2.0", avg_px="25.0")

    def fills(self, payload: dict) -> list[dict]:
        if self.held:
            return []
        fill = fill_entry(oid=CLOSE_OID, tid=1, px="25.0", sz="2.0", time=START_MS + 500)
        return [fill | {"coin": "HYPE", "dir": "Close Short", "startPosition": "-2.0"}]


class _SocketPacedClock(ManualClock):
    """A ``ManualClock`` whose ``sleep`` also lets the socket reader run.

    A plain ``ManualClock`` returns from ``sleep`` without yielding, so flatten's
    first-price wait would always time out before the feed reads a frame. On a
    real clock the venue's subscribe answer arrives well inside that wait.
    """

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(0.001)
        await super().sleep(seconds)


def _flatten(
    tmp_path: Path, venue: _Venue, frames: list[str]
) -> tuple[int, list[MutableMapping[str, Any]], FakeExchangeApi, FakeWsConnection]:
    """Run ``flatten`` on Hyperliquid, faked at HTTP and WebSocket. Config names BTC only."""
    post = FakeExchangeApi(
        {
            "userAbstraction": "disabled",
            "clearinghouseState": venue.account,
            "frontendOpenOrders": [],
            "order": venue.order,
            "userFills": venue.fills,
            "userFillsByTime": venue.fills,
        }
    )
    bus = InMemoryBus()
    clock = _SocketPacedClock(start_ns=START_MS * 1_000_000)
    config = HyperliquidConfig(
        testnet=True, symbols=["BTC"], signing_key=SecretStr(TEST_SIGNING_KEY)
    )
    exchange = HyperliquidExchange(
        config=config,
        bus=bus,
        clock=clock,
        universe=HyperliquidUniverse(
            specs={"BTC": BTC_SPEC, "HYPE": HYPE_SPEC},
            asset_indices={"BTC": 3, "HYPE": HYPE_ASSET},
        ),
        post=post,
        connect=idle_ws,
        startup_timeout_seconds=60.0,
        shrink_wait_seconds=30.0,
    )
    socket = FakeWsConnection(frames)
    engine = Engine(
        bus=bus,
        clock=clock,
        store=SQLiteStore(tmp_path / "saga.db"),
        exchange=exchange,
        feed=HyperliquidFeed(config=config, bus=bus, clock=clock, connect=socket.connect),
        exit_job=Flatten(feed_ends=False),
    )
    with capture_events() as logs:
        exit_code = asyncio.run(asyncio.wait_for(engine.run(), timeout=5))
    return exit_code, logs, post, socket


def test_flatten_subscribes_and_closes_a_held_coin_the_config_never_named(
    tmp_path: Path,
) -> None:
    venue = _Venue()
    # On subscribe the venue sends recent trades and the mark at once (ADR-0059).
    frames = [
        trades_frame(trade("HYPE", "25.0", 7, time=START_MS)),
        asset_ctx_frame("HYPE", "25.0"),
    ]

    exit_code, logs, post, socket = _flatten(tmp_path, venue, frames)

    assert exit_code == 0, [(log["event"], log.get("error")) for log in logs]
    subscribed = {json.loads(m)["subscription"]["coin"] for m in socket.sent}
    assert "HYPE" in subscribed
    (order,) = [
        payload["action"]["orders"][0]
        for url, payload in post.requests
        if request_type(url, payload) == "order"
    ]
    # Buys back the 2.0 short, and can only reduce it.
    assert (order["a"], order["b"], order["s"], order["r"]) == (HYPE_ASSET, True, "2", True)
    assert not [log for log in logs if log["event"] == "flatten.dry_attempt"]
    assert logs[-1]["event"] == "exit.finished"
    assert logs[-1]["outcome"] == "done"


def test_a_coin_with_trades_but_no_mark_is_never_closed(tmp_path: Path) -> None:
    # Hyperliquid caps a market order by the mark. With no mark, the close
    # cannot be priced, so each attempt is dry and no order goes out (#467).
    venue = _Venue()
    frames = [trades_frame(trade("HYPE", "25.0", 7, time=START_MS))]

    exit_code, logs, post, _ = _flatten(tmp_path, venue, frames)

    assert exit_code == 1
    assert not [url for url, payload in post.requests if request_type(url, payload) == "order"]
    dry = [log for log in logs if log["event"] == "flatten.dry_attempt"]
    assert [(log["symbol"], log["count"], log["reason"]) for log in dry] == [
        ("HYPE", 1, "no_price"),
        ("HYPE", 2, "no_price"),
        ("HYPE", 3, "no_price"),
    ]
    assert logs[-1]["event"] == "exit.finished"
    assert logs[-1]["outcome"] == "stopped"
    assert logs[-1]["left"] == "HYPE=-2.0"
