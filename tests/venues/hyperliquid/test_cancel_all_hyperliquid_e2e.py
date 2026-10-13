"""Operator cancel all on Hyperliquid, end to end (issue #470): the real
``Engine`` and ``OperatorCancelAll`` against the real ``HyperliquidExchange``,
with only the HTTP transport fake.

On real money the operator may have placed orders by hand in the venue UI.
Cancel all must pull those too, not only the orders the engine placed.
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
    idle_ws,
    order_status_response,
    request_type,
)
from ledgers import checkpointer
from pydantic import SecretStr

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.feed import ReplayFeed
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    InstrumentSpec,
    Order,
    OrderLive,
    OrderState,
    OrderType,
    Side,
)
from tickwright.engine.exit_run import OperatorCancelAll
from tickwright.engine.runner import Engine
from tickwright.observability.testing import capture_events
from tickwright.venues.hyperliquid import (
    HyperliquidConfig,
    HyperliquidExchange,
    HyperliquidUniverse,
)

BTC_SPEC = InstrumentSpec(
    symbol="BTC",
    sz_decimals=5,
    max_decimals=6,
    min_notional=Decimal("10"),
    max_sig_figs=5,
)

# A flat account with cash only. The engine reads it at boot to stamp genesis.
FLAT_ACCOUNT: dict = {
    "assetPositions": [],
    "crossMaintenanceMarginUsed": "0.0",
    "crossMarginSummary": {
        "accountValue": "100.0",
        "totalMarginUsed": "0.0",
        "totalNtlPos": "0.0",
        "totalRawUsd": "100.0",
    },
    "marginSummary": {
        "accountValue": "100.0",
        "totalMarginUsed": "0.0",
        "totalNtlPos": "0.0",
        "totalRawUsd": "100.0",
    },
    "time": 1_700_000_000_000,
    "withdrawable": "100.0",
}

CLOID = "0x" + "ab" * 16
ACK_MS = 1_700_000_060_000
ENGINE_OID = 901
HAND_PLACED_OID = 902


def _acked_saga() -> Order:
    """The engine's resting order, acked with the venue's oid."""
    order = Order(
        cloid=CLOID,
        strategy_id="live",
        signal_id="live:BTC:1",
        symbol="BTC",
        side=Side.BUY,
        quantity=Decimal("0.5"),
        order_type=OrderType.LIMIT,
    )
    order.state = OrderState.SUBMITTED
    order.apply(
        OrderLive(
            ts_event=ACK_MS * 1_000_000,
            ts_init=ACK_MS * 1_000_000,
            cloid=CLOID,
            strategy_id="live",
            signal_id="live:BTC:1",
            symbol="BTC",
            venue_oid=str(ENGINE_OID),
        )
    )
    return order


def _resting(oid: int, cloid: str | None) -> dict:
    """One ``frontendOpenOrders`` entry. A hand-placed order has a null cloid."""
    return {
        "coin": "BTC",
        "side": "B",
        "limitPx": "40000.0",
        "sz": "0.5",
        "oid": oid,
        "reduceOnly": False,
        "orderType": "Limit",
        "origSz": "0.5",
        "tif": "Gtc",
        "cloid": cloid,
    }


class _VenueBook:
    """The account's resting orders. A cancel by oid takes an order off it."""

    def __init__(self, *, spot: list[dict] | None = None) -> None:
        self.orders = {
            ENGINE_OID: _resting(ENGINE_OID, CLOID),
            HAND_PLACED_OID: _resting(HAND_PLACED_OID, None),
        }
        # Spot orders the read lists beside the perp ones. No cancel reaches them.
        self.spot = spot or []

    def open_orders(self, payload: dict) -> list[dict]:
        return [*self.orders.values(), *self.spot]

    def cancel(self, payload: dict) -> dict:
        cancels = payload["action"]["cancels"]
        for entry in cancels:
            self.orders.pop(entry["o"])
        statuses = ["success"] * len(cancels)
        return {"status": "ok", "response": {"type": "cancel", "data": {"statuses": statuses}}}


def _ticks(path: Path) -> Path:
    # An exit run never starts the feed, but the engine is built with one.
    row = {
        "symbol": "BTC",
        "price": "42000",
        "size": "1",
        "aggressor_side": "buy",
        "trade_id": "a",
        "ts_event": 1_000,
    }
    path.write_text(json.dumps(row) + "\n")
    return path


def _cancel_all(
    tmp_path: Path, book: _VenueBook
) -> tuple[int, list[MutableMapping[str, Any]], FakeExchangeApi]:
    """Run ``cancel-all`` on Hyperliquid, faked at HTTP, over ``book``."""
    post = FakeExchangeApi(
        {
            "userAbstraction": "disabled",
            "clearinghouseState": FLAT_ACCOUNT,
            "orderStatus": order_status_response(
                cloid=CLOID, oid=ENGINE_OID, placed_ms=ACK_MS - 100
            ),
            # The engine order rests unfilled.
            "userFillsByTime": [],
            "frontendOpenOrders": book.open_orders,
            "cancel": book.cancel,
        }
    )
    bus = InMemoryBus()
    clock = ManualClock(start_ns=(ACK_MS + 60_000) * 1_000_000)
    store = SQLiteStore(tmp_path / "saga.db")
    store.checkpoint(_acked_saga(), ts_ns=500)
    exchange = HyperliquidExchange(
        config=HyperliquidConfig(
            testnet=True, symbols=["BTC"], signing_key=SecretStr(TEST_SIGNING_KEY)
        ),
        bus=bus,
        clock=clock,
        universe=HyperliquidUniverse(specs={"BTC": BTC_SPEC}, asset_indices={"BTC": 3}),
        post=post,
        connect=idle_ws,
        startup_timeout_seconds=60.0,
    )
    engine = Engine(
        bus=bus,
        clock=clock,
        store=store,
        exchange=exchange,
        feed=ReplayFeed(path=_ticks(tmp_path / "ticks.jsonl"), bus=bus, clock=clock),
        exit_job=OperatorCancelAll(),
    )

    with capture_events() as logs:
        exit_code = asyncio.run(asyncio.wait_for(engine.run(), timeout=5))
    return exit_code, logs, post


def test_cancel_all_cancels_an_engine_order_and_a_hand_placed_one_and_exits_zero(
    tmp_path: Path,
) -> None:
    book = _VenueBook()

    exit_code, logs, post = _cancel_all(tmp_path, book)

    assert exit_code == 0, [(log["event"], log.get("error")) for log in logs]
    assert book.orders == {}
    # Both go by oid in one request: the engine order because it is acked
    # (#354), the hand-placed one because it has no cloid.
    cancels = [
        payload["action"]["cancels"]
        for url, payload in post.requests
        if request_type(url, payload) == "cancel"
    ]
    assert cancels == [[{"a": 3, "o": ENGINE_OID}, {"a": 3, "o": HAND_PLACED_OID}]]
    assert logs[-1]["event"] == "exit.finished"
    assert logs[-1]["outcome"] == "done"
    # The engine order's saga was marked before the send, so a crash after it
    # cannot leave reconcile to judge the order a ghost.
    reread = checkpointer(SQLiteStore(tmp_path / "saga.db"), clock=ManualClock())
    reread.cache.rebuild()
    saga = reread.cache.get_order(CLOID)
    assert saga is not None
    assert saga.cancel_requested


def test_cancel_all_on_hyperliquid_leaves_a_spot_order_and_still_pulls_the_perp_ones(
    tmp_path: Path,
) -> None:
    # The read lists spot orders too. One used to stop the whole cancel before
    # any request went out, so every perp order stayed resting.
    book = _VenueBook(spot=[{"coin": "@107", "oid": 903, "cloid": None}])

    exit_code, logs, post = _cancel_all(tmp_path, book)

    assert exit_code == 0, [(log["event"], log.get("error")) for log in logs]
    assert book.orders == {}
    skipped = [log for log in logs if log["event"] == "exchange.spot_order_skipped"]
    assert skipped and all(log["coin"] == "@107" and log["oid"] == "903" for log in skipped)
    assert logs[-1]["outcome"] == "done"
