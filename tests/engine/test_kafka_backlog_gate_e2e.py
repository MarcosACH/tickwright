"""An old ``PlaceSignal`` in a Kafka backlog never places before the barrier (issue #563).

The real ``Engine`` on ``KafkaBus`` over the fake broker, with a real
``HyperliquidExchange`` faked at HTTP and WebSocket only. The HTTP fake yields
once per call, like a real network, so ``Exchange.start()`` gives the bus's poll
loop a turn before the barrier runs. That turn is the gap this suite covers.
"""

import asyncio
from decimal import Decimal
from pathlib import Path

from hyperliquid_fakes import TEST_SIGNING_KEY, FakeExchangeApi, idle_ws, resting_response
from kafka_fakes import FakeKafkaBroker
from pydantic import SecretStr

from tickwright.adapters.bus.kafka import KafkaBus
from tickwright.adapters.bus.serde import encode_event
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    Account,
    InstrumentSpec,
    OrderState,
    OrderType,
    PlaceSignal,
    Side,
    TimeInForce,
    derive_cloid,
)
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


def _old_intent() -> PlaceSignal:
    """A strategy order an earlier life published and never handled."""
    return PlaceSignal(
        ts_event=1_000,
        ts_init=1_000,
        strategy_id="trivial",
        symbol="BTC",
        seq=1,
        side=Side.BUY,
        quantity=Decimal("0.001"),
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        price=Decimal("41000"),
    )


def _kafka_bus(broker: FakeKafkaBroker) -> KafkaBus:
    return KafkaBus(
        bootstrap_servers="kafka:9092",
        topic="tickwright.events",
        group_id="tickwright",
        producer_factory=broker.producer,
        consumer_factory=broker.consumer,
    )


def _venue() -> FakeExchangeApi:
    """A flat account with no resting orders, which takes any order it is sent."""
    return FakeExchangeApi(
        {
            "userAbstraction": "disabled",
            "clearinghouseState": FLAT_ACCOUNT,
            "frontendOpenOrders": [],
            "userFills": [],
            "userFillsByTime": [],
            "order": resting_response(901),
        }
    )


def _run_one_life(db: Path, broker: FakeKafkaBroker, post: FakeExchangeApi) -> None:
    """Boot the engine, wait until Kafka has committed every record, then stop.

    The engine closes its store on stop, so each life opens its own on ``db``.
    """
    store = SQLiteStore(db)

    async def network(url: str, payload: dict) -> object:
        # A real request leaves the event loop. This one-turn yield is that.
        await asyncio.sleep(0)
        return await post(url, payload)

    async def life() -> None:
        bus = _kafka_bus(broker)
        clock = ManualClock(start_ns=START_MS * 1_000_000)
        config = HyperliquidConfig(
            testnet=True, symbols=["BTC"], signing_key=SecretStr(TEST_SIGNING_KEY)
        )
        exchange = HyperliquidExchange(
            config=config,
            bus=bus,
            clock=clock,
            universe=HyperliquidUniverse(specs={"BTC": BTC_SPEC}, asset_indices={"BTC": 3}),
            post=network,
            connect=idle_ws,
            startup_timeout_seconds=60.0,
            shrink_wait_seconds=30.0,
        )
        if store.load_account() is None:
            # The life that published the intent also opened the account row.
            store.checkpoint_ledger(
                account=Account(
                    account_id=exchange.account_spec().account_id,
                    genesis_collateral=Decimal("0"),
                    genesis_ts_ns=0,
                ),
                ts_ns=0,
            )
        engine = Engine(
            bus=bus,
            clock=clock,
            store=store,
            exchange=exchange,
            feed=HyperliquidFeed(config=config, bus=bus, clock=clock, connect=idle_ws),
        )
        run = asyncio.create_task(engine.run())
        await broker.all_committed()
        await engine.stop()
        assert await run == 0

    asyncio.run(asyncio.wait_for(life(), timeout=5))


def _sent_orders(post: FakeExchangeApi) -> list[dict]:
    return [
        payload
        for url, payload in post.requests
        if url.endswith("/exchange") and payload["action"]["type"] == "order"
    ]


def test_an_old_intent_in_the_kafka_backlog_is_denied_and_never_placed(tmp_path: Path) -> None:
    db = tmp_path / "saga.db"
    broker = FakeKafkaBroker()
    intent = _old_intent()
    broker.produce(intent.partition_key.encode(), encode_event(intent))
    post = _venue()

    _run_one_life(db, broker, post)

    saga = SQLiteStore(db).get_order(derive_cloid(intent.signal_id))
    assert saga is not None
    assert saga.state is OrderState.DENIED
    assert _sent_orders(post) == []
