"""An old record in a Kafka backlog is never lost, and never placed early.

An old ``PlaceSignal`` never places before the barrier (issue #563). An old
fill, funding payment or mark still reaches its handler (issue #565).

The real ``Engine`` on ``KafkaBus`` over the fake broker, with a real
``HyperliquidExchange`` faked at HTTP and WebSocket only. The HTTP fake yields
once per call, like a real network, so ``Exchange.start()`` gives the bus's poll
loop a turn before the barrier runs. That turn is the gap this suite covers.
"""

import asyncio
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

from eth_account import Account as Wallet
from hyperliquid_fakes import (
    TEST_SIGNING_KEY,
    FakeExchangeApi,
    idle_ws,
    order_status_response,
    request_type,
    resting_response,
)
from kafka_fakes import FakeKafkaBroker
from pydantic import SecretStr

from tickwright.adapters.bus.kafka import KafkaBus
from tickwright.adapters.bus.serde import encode_event
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    Account,
    CancelSignal,
    ComponentState,
    Event,
    FillReport,
    FundingAccrual,
    InstrumentSpec,
    MarkTick,
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
ACCOUNT_ID = f"hyperliquid-testnet-{Wallet.from_key(TEST_SIGNING_KEY).address}"
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


def _venue(**routes: object) -> FakeExchangeApi:
    """A flat account with no resting orders, which takes any order it is sent.

    ``routes`` adds or replaces answers, keyed by request type.
    """
    return FakeExchangeApi(
        {
            "userAbstraction": "disabled",
            "clearinghouseState": FLAT_ACCOUNT,
            "frontendOpenOrders": [],
            "userFills": [],
            "userFillsByTime": [],
            "order": resting_response(901),
            **routes,
        }
    )


def _produce(broker: FakeKafkaBroker, event: Event) -> None:
    broker.produce(event.partition_key.encode(), encode_event(event))


def _run_one_life(
    db: Path,
    broker: FakeKafkaBroker,
    post: FakeExchangeApi,
    *,
    after_barrier: Sequence[Event] = (),
) -> Engine:
    """Boot the engine, wait until Kafka has committed every record, then stop.

    ``after_barrier`` lands on the broker once the engine is ``RUNNING``. The
    engine closes its store on stop, so each life opens its own on ``db``.
    Returns the stopped engine, so a test can read what it held in memory.
    """
    store = SQLiteStore(db)

    async def network(url: str, payload: dict) -> object:
        # A real request leaves the event loop. This one-turn yield is that.
        await asyncio.sleep(0)
        return await post(url, payload)

    async def life() -> Engine:
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
        while engine.state is not ComponentState.RUNNING:
            await asyncio.sleep(0)
        for event in after_barrier:
            _produce(broker, event)
        await broker.all_committed()
        await engine.stop()
        assert await run == 0
        return engine

    return asyncio.run(asyncio.wait_for(life(), timeout=5))


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
    _produce(broker, intent)
    post = _venue()

    _run_one_life(db, broker, post)

    saga = SQLiteStore(db).get_order(derive_cloid(intent.signal_id))
    assert saga is not None
    assert saga.state is OrderState.DENIED
    assert _sent_orders(post) == []


def test_a_later_life_never_places_an_intent_an_earlier_life_denied(tmp_path: Path) -> None:
    # The first life denies the backlog intent. The second life hears it again
    # after its barrier, where the gate is open. Only the DENIED row stops it.
    db = tmp_path / "saga.db"
    broker = FakeKafkaBroker()
    intent = _old_intent()
    _produce(broker, intent)
    _run_one_life(db, broker, _venue())
    post = _venue()

    _run_one_life(db, broker, post, after_barrier=[intent])

    saga = SQLiteStore(db).get_order(derive_cloid(intent.signal_id))
    assert saga is not None
    assert saga.state is OrderState.DENIED
    assert _sent_orders(post) == []


def test_an_intent_that_arrives_after_the_barrier_still_places(tmp_path: Path) -> None:
    db = tmp_path / "saga.db"
    intent = _old_intent()
    post = _venue()

    _run_one_life(db, FakeKafkaBroker(), post, after_barrier=[intent])

    saga = SQLiteStore(db).get_order(derive_cloid(intent.signal_id))
    assert saga is not None
    assert saga.state is OrderState.LIVE
    (sent,) = _sent_orders(post)
    assert sent["action"]["orders"][0]["c"] == derive_cloid(intent.signal_id)


def test_an_old_cancel_in_the_kafka_backlog_still_reaches_the_venue(tmp_path: Path) -> None:
    # A cancel is never denied at boot. It only lowers exposure, so it may go
    # out before the account-mode gate has answered (ADR-0046 §3).
    db = tmp_path / "saga.db"
    intent = _old_intent()
    _run_one_life(db, FakeKafkaBroker(), _venue(), after_barrier=[intent])
    broker = FakeKafkaBroker()
    _produce(
        broker,
        CancelSignal(
            ts_event=2_000,
            ts_init=2_000,
            strategy_id="trivial",
            symbol="BTC",
            seq=2,
            target_signal_id=intent.signal_id,
        ),
    )
    cloid = derive_cloid(intent.signal_id)
    post = _venue(
        cancel={"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success"]}}},
        orderStatus=order_status_response(cloid=cloid, status="canceled", oid=901),
    )

    _run_one_life(db, broker, post)

    # Sent while the engine is still at boot, right behind the mode read.
    sent = [request_type(url, payload) for url, payload in post.requests]
    assert sent[:2] == ["userAbstraction", "cancel"]
    saga = SQLiteStore(db).get_order(cloid)
    assert saga is not None
    assert saga.state is OrderState.CANCELLED


def test_an_old_funding_payment_in_the_kafka_backlog_is_booked(tmp_path: Path) -> None:
    db = tmp_path / "saga.db"
    broker = FakeKafkaBroker()
    _produce(
        broker,
        FundingAccrual(
            ts_event=1_000,
            ts_init=1_000,
            account_id=ACCOUNT_ID,
            symbol="BTC",
            boundary_ts_ns=1_000,
            amount=Decimal("-1.5"),
        ),
    )

    _run_one_life(db, broker, _venue())

    account = SQLiteStore(db).load_account()
    assert account is not None
    assert account.cash == Decimal("-1.5")


def test_an_old_fill_in_the_kafka_backlog_is_booked(tmp_path: Path) -> None:
    db = tmp_path / "saga.db"
    intent = _old_intent()
    cloid = derive_cloid(intent.signal_id)
    _run_one_life(db, FakeKafkaBroker(), _venue(), after_barrier=[intent])
    broker = FakeKafkaBroker()
    _produce(
        broker,
        FillReport(
            ts_event=2_000,
            ts_init=2_000,
            cloid=cloid,
            symbol="BTC",
            trade_id="5001",
            quantity=Decimal("0.0005"),
            price=Decimal("41000"),
        ),
    )
    # The venue lists no fills, so reconcile cannot book this one for us.
    # Only the backlog record can.
    post = _venue(orderStatus=order_status_response(cloid=cloid, oid=901))

    _run_one_life(db, broker, post)

    saga = SQLiteStore(db).get_order(cloid)
    assert saga is not None
    assert saga.state is OrderState.PARTIALLY_FILLED
    assert saga.fill_count == 1


def test_an_old_mark_in_the_kafka_backlog_reaches_the_ledger(tmp_path: Path) -> None:
    broker = FakeKafkaBroker()
    mark = MarkTick(ts_event=1_000, ts_init=1_000, symbol="BTC", price=Decimal("41234"))
    _produce(broker, mark)

    engine = _run_one_life(tmp_path / "saga.db", broker, _venue())

    assert engine.portfolio.latest_mark("BTC") == mark
