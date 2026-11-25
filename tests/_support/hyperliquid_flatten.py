"""A flatten run on Hyperliquid, faked at HTTP and WebSocket only (issue #475).

The real ``Engine``, ``Flatten``, ``HyperliquidExchange`` and ``HyperliquidFeed``.
The account holds a HYPE short opened by hand. The config names BTC only.
Shared by the flatten e2e tests and the catalog walk.
"""

import asyncio
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

from hyperliquid_fakes import (
    ORACLE_REJECT,
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
from tickwright.domain import Account, InstrumentSpec, Position
from tickwright.engine.exit_run import Flatten
from tickwright.engine.runner import Engine, EngineConfig
from tickwright.venues.hyperliquid import (
    HyperliquidConfig,
    HyperliquidExchange,
    HyperliquidFeed,
    HyperliquidUniverse,
)
from tickwright.venues.hyperliquid.transport import PostJson

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


class _SocketPacedClock(ManualClock):
    """A ``ManualClock`` whose ``sleep`` also lets the socket reader run.

    A plain ``ManualClock`` returns from ``sleep`` without yielding, so flatten's
    first-price wait would always time out before the feed reads a frame. On a
    real clock the venue's subscribe answer arrives well inside that wait.
    """

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(0.001)
        await super().sleep(seconds)


class HeldShort:
    """The account: a HYPE short opened by hand, flat once a close fills it.

    It owns the run's clock, so a fake can date the requests it answers.
    """

    def __init__(self) -> None:
        self.held = True
        self.clock: ManualClock = _SocketPacedClock(start_ns=START_MS * 1_000_000)

    def account(self, payload: dict) -> dict:
        return _account(_short("HYPE", "-2.0", "25.0")) if self.held else _account()

    def order(self, payload: dict) -> object:
        self.held = False
        return filled_response(oid=CLOSE_OID, total_sz="2.0", avg_px="25.0")

    def order_status(self, payload: dict) -> dict:
        # A refused close never reached the book, so the read by cloid finds no record.
        return {"status": "unknownOid"}

    def fills(self, payload: dict) -> list[dict]:
        if self.held:
            return []
        fill = fill_entry(oid=CLOSE_OID, tid=1, px="25.0", sz="2.0", time=START_MS + 500)
        return [fill | {"coin": "HYPE", "dir": "Close Short", "startPosition": "-2.0"}]


class AnswersFirst(HeldShort):
    """Gives ``answer`` for the first close and never takes it, then fills the next one.

    ``answer`` is a venue body, or an exception the send raises.
    """

    def __init__(self, answer: object) -> None:
        super().__init__()
        self._answer: object | None = answer

    def order(self, payload: dict) -> object:
        answer, self._answer = self._answer, None
        return answer if answer is not None else super().order(payload)


class HalfClosesBesideAGap(HeldShort):
    """Each HYPE close fills half the short. A BTC long the books never hold sits beside it.

    The BTC size grows by 0.001 on every read. An account pass heals a size gap
    only once two passes see the same gap (#574), so no pass ever books BTC.
    BTC's loop then runs an account pass on every attempt.
    """

    def __init__(self) -> None:
        super().__init__()
        self.closes = 0
        self.btc_reads = 0

    def account(self, payload: dict) -> dict:
        self.btc_reads += 1
        btc = _short("BTC", str(Decimal("0.001") * self.btc_reads), "0.0")
        hype = Decimal("-2.0") + self.closes
        return _account(btc, *([_short("HYPE", str(hype), "25.0")] if hype else []))

    def order(self, payload: dict) -> object:
        self.closes += 1
        return filled_response(oid=CLOSE_OID + self.closes, total_sz="1.0", avg_px="25.0")

    def fills(self, payload: dict) -> list[dict]:
        return [
            fill_entry(oid=CLOSE_OID + n, tid=n, px="25.0", sz="1.0", time=START_MS + 500 * n)
            | {"coin": "HYPE", "dir": "Close Short", "startPosition": str(Decimal(n - 3))}
            for n in range(1, self.closes + 1)
        ]


class _TurnPacedClock(ManualClock):
    """A ``ManualClock`` whose ``sleep`` lets the loop run many turns before time moves.

    The fakes do no real IO, so every answer takes a few loop turns. A wait that
    something can end early then ends early on any machine. ``_SocketPacedClock``
    yields 1 ms of real time instead, which a slow store write can use up.
    """

    async def sleep(self, seconds: float) -> None:
        for _ in range(500):
            await asyncio.sleep(0)
        await super().sleep(seconds)


class BookedLongBesideAGap(HeldShort):
    """A BTC long the books hold, beside the HYPE short opened by hand.

    The venue values the BTC long at 65309. Its uPnL is 0.002 x (65309 - 64809).
    ``closed_at`` is how long after the start each close came in, on the run's clock.
    """

    BOOKED = Position(
        strategy_id="opened",
        symbol="BTC",
        signed_size=Decimal("0.002"),
        entry_price=Decimal("64809"),
    )

    def __init__(self) -> None:
        super().__init__()
        self.clock = _TurnPacedClock(start_ns=START_MS * 1_000_000)
        self.closed_at: dict[str, int] = {}
        self._filled: list[dict] = []

    def account(self, payload: dict) -> dict:
        btc = _short("BTC", "0.002", "64809.0")
        btc["position"] |= {"positionValue": "130.618", "unrealizedPnl": "1.0"}
        hype = _short("HYPE", "-2.0", "25.0")
        return _account(
            *(row for row in (btc, hype) if row["position"]["coin"] not in self.closed_at)
        )

    def order(self, payload: dict) -> object:
        order = payload["action"]["orders"][0]
        coin, size, price, held, side = (
            ("HYPE", "2.0", "25.0", "-2.0", "B")
            if order["a"] == HYPE_ASSET
            else ("BTC", "0.002", "65000.0", "0.002", "A")
        )
        self.closed_at[coin] = self.clock.timestamp_ns() - START_MS * 1_000_000
        oid = CLOSE_OID + len(self._filled)
        fill = fill_entry(oid=oid, tid=oid, px=price, sz=size, time=START_MS + 500)
        self._filled.append(fill | {"coin": coin, "side": side, "startPosition": held})
        return filled_response(oid=oid, total_sz=size, avg_px=price)

    def fills(self, payload: dict) -> list[dict]:
        return self._filled


class FillsHalfAfterOracleRejects(HeldShort):
    """Rejects closes against its oracle, but fills half the short once ``fill_after_s`` passed.

    Every close after that fill is rejected against the oracle again.
    """

    def __init__(self, *, fill_after_s: float) -> None:
        super().__init__()
        self._fill_at_ns = self.clock.timestamp_ns() + int(fill_after_s * 1_000_000_000)
        self.filled_ms: int | None = None

    def account(self, payload: dict) -> dict:
        size = "-2.0" if self.filled_ms is None else "-1.0"
        return _account(_short("HYPE", size, "25.0"))

    def order(self, payload: dict) -> object:
        if self.filled_ms is not None or self.clock.timestamp_ns() < self._fill_at_ns:
            return ORACLE_REJECT
        self.filled_ms = self.clock.timestamp_ns() // 1_000_000
        return filled_response(oid=CLOSE_OID, total_sz="1.0", avg_px="25.0")

    def fills(self, payload: dict) -> list[dict]:
        if self.filled_ms is None:
            return []
        fill = fill_entry(oid=CLOSE_OID, tid=1, px="25.0", sz="1.0", time=self.filled_ms)
        return [fill | {"coin": "HYPE", "dir": "Close Short", "startPosition": "-2.0"}]


def run_flatten(
    store: Path | str,
    venue: HeldShort,
    frames: list[str],
    *,
    engine_config: EngineConfig | None = None,
    transport: Callable[[FakeExchangeApi], PostJson] | None = None,
    booked: tuple[Position, ...] = (),
) -> tuple[int, FakeExchangeApi, FakeWsConnection]:
    """Run ``flatten`` once and return its exit code, the HTTP fake and the socket fake.

    ``engine_config`` lets a test change the reconcile timing. ``None`` keeps the defaults.
    ``transport`` wraps the HTTP fake, so a test can hold an answer open.
    ``booked`` is what the ledger already holds at boot.
    """
    post = FakeExchangeApi(
        {
            "userAbstraction": "disabled",
            "clearinghouseState": venue.account,
            "frontendOpenOrders": [],
            "order": venue.order,
            "orderStatus": venue.order_status,
            "userFills": venue.fills,
            "userFillsByTime": venue.fills,
        }
    )
    bus = InMemoryBus()
    clock = venue.clock
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
        post=post if transport is None else transport(post),
        connect=idle_ws,
        startup_timeout_seconds=60.0,
        cut_part_wait_seconds=30.0,
    )
    socket = FakeWsConnection(frames)
    used = SQLiteStore(store)
    # The engine that opened the position also opened the account row (ADR-0052).
    if used.load_account() is None:
        used.checkpoint_ledger(
            account=Account(
                account_id=exchange.account_spec().account_id,
                genesis_collateral=Decimal("0"),
                genesis_ts_ns=0,
            ),
            positions=booked,
            ts_ns=0,
        )
    engine = Engine(
        bus=bus,
        clock=clock,
        store=used,
        exchange=exchange,
        feed=HyperliquidFeed(config=config, bus=bus, clock=clock, connect=socket.connect),
        config=engine_config,
        exit_job=Flatten(feed_ends=False),
    )
    # No cap here. The suite timeout in pyproject.toml ends a hung run (#510). A 5 s cap
    # here failed runs that were only slow on a busy runner (#623).
    exit_code = asyncio.run(engine.run())
    return exit_code, post, socket
