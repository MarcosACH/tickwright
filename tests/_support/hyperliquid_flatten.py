"""A flatten run on Hyperliquid, faked at HTTP and WebSocket only (issue #475).

The real ``Engine``, ``Flatten``, ``HyperliquidExchange`` and ``HyperliquidFeed``.
The account holds a HYPE short opened by hand. The config names BTC only.
Shared by the flatten e2e tests and the catalog walk.
"""

import asyncio
from decimal import Decimal
from pathlib import Path

from hyperliquid_fakes import (
    TEST_SIGNING_KEY,
    FakeExchangeApi,
    FakeWsConnection,
    asset_ctx_frame,
    fill_entry,
    filled_response,
    idle_ws,
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
# The venue's own words, from a testnet probe
# (docs/research/hyperliquid-rate-limit-window-probe.md).
ADDRESS_LIMIT = (
    "Too many cumulative requests sent (97460 > 97456) for cumulative volume traded"
    " $87457.46. Place taker orders to free up 1 request per USDC traded."
)

_SUMMARY = {
    "accountValue": "100.0",
    "totalMarginUsed": "0.0",
    "totalNtlPos": "0.0",
    "totalRawUsd": "100.0",
}


def priced_frames() -> list[str]:
    """What the venue sends on subscribe: recent trades and the mark at once (ADR-0059)."""
    return [
        trades_frame(trade("HYPE", "25.0", 7, time=START_MS)),
        asset_ctx_frame("HYPE", "25.0"),
    ]


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


class HeldShort:
    """The account: a HYPE short opened by hand, flat once a close fills it."""

    def __init__(self) -> None:
        self.held = True

    def account(self, payload: dict) -> dict:
        return _account(_short("HYPE", "-2.0", "25.0")) if self.held else _account()

    def order(self, payload: dict) -> object:
        self.held = False
        return filled_response(oid=CLOSE_OID, total_sz="2.0", avg_px="25.0")

    def fills(self, payload: dict) -> list[dict]:
        if self.held:
            return []
        fill = fill_entry(oid=CLOSE_OID, tid=1, px="25.0", sz="2.0", time=START_MS + 500)
        return [fill | {"coin": "HYPE", "dir": "Close Short", "startPosition": "-2.0"}]


class RateLimitedOnce(HeldShort):
    """Refuses the first close for a rate limit, then fills the next one."""

    def __init__(self, refusal: object) -> None:
        super().__init__()
        self._refusal: object | None = refusal

    def order(self, payload: dict) -> object:
        refusal, self._refusal = self._refusal, None
        return refusal if refusal is not None else super().order(payload)


class _SocketPacedClock(ManualClock):
    """A ``ManualClock`` whose ``sleep`` also lets the socket reader run.

    A plain ``ManualClock`` returns from ``sleep`` without yielding, so flatten's
    first-price wait would always time out before the feed reads a frame. On a
    real clock the venue's subscribe answer arrives well inside that wait.
    """

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(0.001)
        await super().sleep(seconds)


def run_flatten(
    store: Path | str, venue: HeldShort, frames: list[str]
) -> tuple[int, FakeExchangeApi, FakeWsConnection]:
    """Run ``flatten`` once and return its exit code, the HTTP fake and the socket fake."""
    post = FakeExchangeApi(
        {
            "userAbstraction": "disabled",
            "clearinghouseState": venue.account,
            "frontendOpenOrders": [],
            "order": venue.order,
            # A refused close never reached the book, so the read by cloid finds no record.
            "orderStatus": {"status": "unknownOid"},
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
        store=SQLiteStore(store),
        exchange=exchange,
        feed=HyperliquidFeed(config=config, bus=bus, clock=clock, connect=socket.connect),
        exit_job=Flatten(feed_ends=False),
    )
    exit_code = asyncio.run(asyncio.wait_for(engine.run(), timeout=5))
    return exit_code, post, socket
