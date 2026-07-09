"""``KafkaBus`` — the same ADR-0023 topology over one keyed topic (ADR-0028).

The Kafka client is faked at the process boundary (ADR-0022): ``FakeKafkaBroker``
stands in for the cluster behind aiokafka's producer/consumer surface, with real
partitioning by key hash and per-partition offsets; everything on our side of
that line — serde, keying, dispatch, commits — is real ``KafkaBus`` code.
"""

import asyncio
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from kafka_fakes import FakeKafkaBroker

from tickwright.adapters.bus.kafka import KafkaBus
from tickwright.adapters.bus.serde import decode_event
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    Event,
    EventBus,
    ExecutionReport,
    MarketTick,
    OrderEvent,
    OrderState,
    OrderType,
    PlaceSignal,
    Side,
    Signal,
    TimeInForce,
    derive_cloid,
)
from tickwright.domain.enums import AggressorSide
from tickwright.engine.cache import Cache
from tickwright.engine.execution import ExecutionManager


def _tick(seq: int = 1, symbol: str = "BTC", price: str = "100") -> MarketTick:
    return MarketTick(
        ts_event=seq,
        ts_init=seq,
        symbol=symbol,
        price=Decimal(price),
        size=Decimal("1"),
        aggressor_side=AggressorSide.BUY,
        trade_id=f"t{seq}",
        seq=seq,
    )


def _wire(broker: FakeKafkaBroker) -> KafkaBus:
    return KafkaBus(
        bootstrap_servers="kafka:9092",
        topic="tickwright.events",
        group_id="tickwright",
        producer_factory=broker.producer,
        consumer_factory=broker.consumer,
    )


def test_kafka_bus_satisfies_the_eventbus_seam() -> None:
    assert isinstance(_wire(FakeKafkaBroker()), EventBus)


def test_publish_sends_one_encoded_record_keyed_by_partition_key() -> None:
    broker = FakeKafkaBroker()
    bus = _wire(broker)
    tick = _tick(symbol="ETH")

    async def scenario() -> None:
        await bus.start()
        await bus.publish(tick)
        await bus.close()

    asyncio.run(scenario())

    partition = broker.partitions[broker.partition_for(b"ETH")]
    assert len(partition) == 1
    key, value = partition[0]
    assert key == b"ETH"
    assert decode_event(value) == tick


def test_subscribed_handler_receives_events_consumed_from_the_topic() -> None:
    broker = FakeKafkaBroker()
    bus = _wire(broker)
    seen: list[Event] = []

    async def handler(event: MarketTick) -> None:
        seen.append(event)

    bus.subscribe(MarketTick, handler)

    async def scenario() -> None:
        await bus.start()
        await bus.publish(_tick(1))
        await bus.publish(_tick(2))
        await broker.all_committed()
        await bus.close()

    asyncio.run(scenario())

    # Same symbol -> same partition -> delivered in publish order, and
    # `all_committed` returning proves offsets advanced only after dispatch.
    assert seen == [_tick(1), _tick(2)]


def test_a_handler_fault_propagates_into_the_causing_publish_and_the_bus_survives() -> None:
    # Containment parity (ADR-0024): on InMemoryBus a raw handler exception
    # propagates into the publish that caused it, once, and the bus keeps
    # working. Over Kafka dispatch happens in the poll loop, so the fault is
    # handed to the publish draining that cascade; the record is dropped
    # uncommitted (a restart would redeliver it) and later publishes deliver.
    broker = FakeKafkaBroker()
    bus = _wire(broker)
    seen: list[MarketTick] = []

    async def brittle(event: MarketTick) -> None:
        if event.seq == 1:
            raise RuntimeError("handler broke an engine assumption")
        seen.append(event)

    bus.subscribe(MarketTick, brittle)

    async def scenario() -> None:
        await bus.start()
        with pytest.raises(RuntimeError, match="handler broke"):
            await bus.publish(_tick(1))
        await bus.publish(_tick(2))
        await bus.drain()
        await bus.close()  # teardown still works after a fault

    # Bounded: a wrong implementation must fail loudly, not hang in drain.
    asyncio.run(asyncio.wait_for(scenario(), timeout=5))

    assert seen == [_tick(2)]


def test_when_two_same_symbol_records_fault_the_first_is_the_one_that_surfaces() -> None:
    # Containment parity, tightened (ADR-0024): InMemoryBus dispatches in FIFO
    # order and aborts on the *first* fault, so the first-published exception is
    # the one a caller sees. Two same-symbol records share a partition, so the
    # poll loop dispatches them in publish order — and both are already produced
    # (seeded reentrantly) before the draining publisher gets to re-raise, so
    # the loop hits both faults in one pass. The fault slot must keep the first,
    # not let the second overwrite it, or the two backends report different
    # exceptions for the same input (a per-symbol-ordered parity break).
    broker = FakeKafkaBroker()
    bus = _wire(broker)

    async def handler(event: MarketTick) -> None:
        if event.symbol == "seed":
            await bus.publish(_tick(1, symbol="BTC"))  # reentrant: produced, not dispatched
            await bus.publish(_tick(2, symbol="BTC"))
        elif event.seq == 1:
            raise RuntimeError("first fault")
        elif event.seq == 2:
            raise RuntimeError("second fault")

    bus.subscribe(MarketTick, handler)

    async def scenario() -> None:
        await bus.start()
        with pytest.raises(RuntimeError, match="first fault"):
            await bus.publish(_tick(seq=0, symbol="seed"))
        await bus.close()  # teardown still works after the fault

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_drain_returns_only_after_every_published_event_was_dispatched() -> None:
    # The ADR-0024 shutdown drain: over Kafka "the FIFO went idle" means every
    # record this process produced has been delivered and committed — including
    # records a handler published mid-drain (the reentrant cascade tail).
    broker = FakeKafkaBroker()
    bus = _wire(broker)
    seen: list[MarketTick] = []

    async def cascade(event: MarketTick) -> None:
        seen.append(event)
        if event.seq == 1:
            await bus.publish(_tick(2, symbol="ETH"))

    bus.subscribe(MarketTick, cascade)

    async def scenario() -> None:
        await bus.start()
        await bus.publish(_tick(1, symbol="BTC"))
        await bus.drain()
        # No sleeps, no fake-broker helpers: drain alone must be the fence.
        assert seen == [_tick(1, symbol="BTC"), _tick(2, symbol="ETH")]
        assert broker.committed == [len(p) for p in broker.partitions]
        await bus.close()

    asyncio.run(scenario())


# ---- Property: per-symbol ordering across the keyed topic (ADR-0028) --------
#
# The bus promises per-symbol ordering *only*: a symbol's whole chain lands on
# one partition, so its events arrive in publish order; cross-symbol order is
# free to scramble as the poll loop round-robins partitions. The sequence is
# published from inside a handler (the reentrant path) so the records genuinely
# interleave across partitions instead of each publish draining before the next.

_SYMBOLS = ["BTC", "ETH", "SOL", "DOGE", "ADA"]


@given(plan=st.lists(st.sampled_from(_SYMBOLS), max_size=24))
def test_events_for_one_symbol_arrive_in_publish_order(plan: list[str]) -> None:
    broker = FakeKafkaBroker()
    bus = _wire(broker)
    ticks = [_tick(seq=index + 1, symbol=symbol) for index, symbol in enumerate(plan)]
    seen: list[MarketTick] = []

    async def seed_then_record(event: MarketTick) -> None:
        if event.symbol == "seed":
            for tick in ticks:
                await bus.publish(tick)  # reentrant: produced, not yet dispatched
        else:
            seen.append(event)

    bus.subscribe(MarketTick, seed_then_record)

    async def scenario() -> None:
        await bus.start()
        await bus.publish(_tick(seq=0, symbol="seed"))
        await bus.drain()
        await bus.close()

    asyncio.run(scenario())

    assert sorted(seen, key=lambda t: t.seq) == ticks  # delivered exactly once each
    for symbol in _SYMBOLS:
        published = [tick.seq for tick in ticks if tick.symbol == symbol]
        delivered = [tick.seq for tick in seen if tick.symbol == symbol]
        assert delivered == published


# ---- Property: rewound offsets redeliver, final state converges (ADR-0002) --
#
# At-least-once delivery is the shared bus contract: a consumer whose offsets
# rewind re-dispatches everything after the rewind point, and the saga's
# idempotency keys must swallow every replayed record — the durable end state
# and the canonical OrderEvent stream are exactly the no-rewind baseline's.

_CLOID = derive_cloid("trivial:BTC:1")


def _saga_pipeline(broker: FakeKafkaBroker) -> tuple[KafkaBus, SQLiteStore, list[OrderEvent]]:
    bus = _wire(broker)
    clock = ManualClock(start_ns=1_000)
    store = SQLiteStore(":memory:")
    exchange = PaperExchange(bus=bus, clock=clock, fill_model=ImmediateFillModel())
    manager = ExecutionManager(bus=bus, clock=clock, exchange=exchange, cache=Cache(store=store))
    bus.subscribe(Signal, manager.on_signal)
    bus.subscribe(ExecutionReport, manager.on_execution_report)

    order_events: list[OrderEvent] = []

    async def record(event: OrderEvent) -> None:
        order_events.append(event)

    bus.subscribe(OrderEvent, record)
    return bus, store, order_events


def _place_signal() -> PlaceSignal:
    return PlaceSignal(
        ts_event=1_000,
        ts_init=1_000,
        strategy_id="trivial",
        symbol="BTC",
        seq=1,
        side=Side.BUY,
        quantity=Decimal("0.5"),
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.IOC,
    )


def _run_saga_with_rewind(rewind_fraction: float | None) -> tuple[SQLiteStore, list[OrderEvent]]:
    broker = FakeKafkaBroker()
    bus, store, order_events = _saga_pipeline(broker)

    async def scenario() -> None:
        await bus.start()
        await bus.publish(_tick(seq=0, symbol="BTC", price="42000"))
        await bus.publish(_place_signal())  # cascades to FILLED, all committed
        if rewind_fraction is not None:
            positions = [int(len(p) * rewind_fraction) for p in broker.partitions]
            broker.consumers[0].rewind(positions)
            # Everything from the rewind point redelivers; the broker fence
            # holds until the replay is fully reprocessed and recommitted.
            await broker.all_committed()
        await bus.close()

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))
    return store, order_events


@given(rewind_fraction=st.floats(min_value=0.0, max_value=1.0))
def test_rewound_offsets_redeliver_without_changing_final_state(rewind_fraction: float) -> None:
    baseline_store, baseline_events = _run_saga_with_rewind(None)
    store, order_events = _run_saga_with_rewind(rewind_fraction)

    # At-least-once is the shared contract: raw subscribers may see replayed
    # copies, so the *stream* is allowed to repeat — what must not change is
    # anything an idempotency key guards.
    record = store.get_order(_CLOID)
    baseline = baseline_store.get_order(_CLOID)
    assert record is not None and baseline is not None
    assert record.state is baseline.state is OrderState.FILLED
    assert record.cum_qty == baseline.cum_qty == Decimal("0.5")
    # The durable trail is exactly the baseline's: no duplicated checkpoint,
    # no re-placement (a second send would mint a second distinct trade id).
    assert [state for state, _ in store.history(_CLOID)] == [
        state for state, _ in baseline_store.history(_CLOID)
    ]

    # Deduplicating the delivered stream on event_id recovers the canonical
    # sequence — every replayed copy collapsed onto an already-seen key.
    def _dedup(events: list[OrderEvent]) -> list[tuple[str, str]]:
        seen: dict[str, str] = {}
        for ev in events:
            seen.setdefault(ev.event_id, type(ev).__name__)
        return [(name, event_id) for event_id, name in seen.items()]

    assert _dedup(order_events) == _dedup(baseline_events)
