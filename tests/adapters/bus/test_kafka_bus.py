"""``KafkaBus`` — the same ADR-0023 topology over one keyed topic (ADR-0028).

The Kafka client is faked at the process boundary (ADR-0022): ``FakeKafkaBroker``
stands in for the cluster behind aiokafka's producer/consumer surface, with real
partitioning by key hash and per-partition offsets; everything on our side of
that line — serde, keying, dispatch, commits — is real ``KafkaBus`` code.

The cascade rules both backends share are pinned once, in ``test_bus_parity.py``.
This file keeps what only the topic can show: the wire record, offset commits,
where a held record lands, and a send that fails.
"""

import asyncio
from decimal import Decimal

import msgspec
import pytest
from hypothesis import given
from hypothesis import strategies as st
from kafka_fakes import FakeKafkaBroker, Record
from ledgers import GENESIS, checkpointer

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


def test_a_dispatch_fault_drops_what_its_handlers_published_before_it_reaches_the_topic() -> None:
    """The Kafka face of InMemoryBus clearing its FIFO on a fault (ADR-0023).
    A record a handler published is still held when a later step raises, so
    it never lands, not even on the next dispatch."""
    broker = FakeKafkaBroker()
    bus = _wire(broker)
    delivered: list[MarketTick] = []

    async def handler(event: MarketTick) -> None:
        if event.symbol == "trigger":
            await bus.publish(_tick(1, symbol="BTC"))
            raise RuntimeError("boom")
        delivered.append(event)

    bus.subscribe(MarketTick, handler)

    async def scenario() -> None:
        await bus.start()
        with pytest.raises(RuntimeError, match="boom"):
            await bus.publish(_tick(seq=0, symbol="trigger"))
        await bus.publish(_tick(2, symbol="ETH"))  # a later dispatch must not flush it
        await bus.drain()
        await bus.close()

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))

    assert delivered == [_tick(2, symbol="ETH")]
    on_topic = [decode_event(value) for p in broker.partitions for _, value in p]
    assert _tick(1, symbol="BTC") not in on_topic


def test_a_send_that_fails_after_the_handler_returned_is_a_dispatch_fault() -> None:
    """The held record is sent after the dispatch, so a broker that went
    away surfaces in the poll loop, not inside a strategy callback where the
    containment net would log it as a strategy bug. It is a dispatch fault:
    the causing publish raises, the trigger stays uncommitted so a restart
    redelivers it, and the bus keeps serving once the broker is back."""
    broker = FakeKafkaBroker()
    bus = _wire(broker)
    delivered: list[MarketTick] = []

    async def handler(event: MarketTick) -> None:
        delivered.append(event)
        if event.symbol == "trigger":
            await bus.publish(_tick(1, symbol="BTC"))
            broker.producers[-1].failure = ConnectionError("broker went away")

    bus.subscribe(MarketTick, handler)

    async def scenario() -> None:
        await bus.start()
        with pytest.raises(ConnectionError, match="broker went away"):
            await bus.publish(_tick(seq=0, symbol="trigger"))
        assert broker.committed == [0] * broker.partition_count
        broker.producers[-1].failure = None
        await bus.publish(_tick(2, symbol="ETH"))
        await bus.drain()
        await bus.close()

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))

    assert delivered == [_tick(seq=0, symbol="trigger"), _tick(2, symbol="ETH")]


def test_a_reentrant_publish_lands_after_its_dispatch_returns_and_before_its_commit() -> None:
    """A record a handler publishes is held until the dispatch that published
    it returns, then sent, then the triggering offset is committed (issue
    #350). Sending at once would make the record durable before the handler's
    own durable writes, which is how a Signal outlived its snapshot on Kafka.
    Sending after the commit would lose it on a crash in between, with the
    trigger never redelivered."""
    broker = FakeKafkaBroker()
    bus = _wire(broker)
    trigger = _tick(seq=0, symbol="BTC")
    inner = _tick(seq=1, symbol="BTC")
    on_topic_inside_handler: list[int] = []
    committed_when_inner_landed: list[list[int]] = []

    def observe(record: Record) -> None:
        if decode_event(record.value) == inner:
            committed_when_inner_landed.append(list(broker.committed))

    broker.on_produce.append(observe)

    async def handler(event: MarketTick) -> None:
        if event == trigger:
            await bus.publish(inner)
            on_topic_inside_handler.append(sum(len(p) for p in broker.partitions))

    bus.subscribe(MarketTick, handler)

    async def scenario() -> None:
        await bus.start()
        await bus.publish(trigger)
        await bus.drain()
        await bus.close()

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))

    assert on_topic_inside_handler == [1]  # only the trigger, the inner one is held
    partition = broker.partition_for(b"BTC")
    assert committed_when_inner_landed == [[0] * broker.partition_count]
    assert broker.committed[partition] == 2  # both delivered and committed in the end


def test_a_malformed_record_surfaces_as_a_fault_instead_of_hanging_the_drain() -> None:
    # A payload that will not decode must not silently kill the poll loop and
    # hang drain forever (the fetch position advances, so a naive loop would
    # wait on a commit that never comes). It is handled like any dispatch fault:
    # dropped, its offset advanced, the error handed to the draining publisher.
    broker = FakeKafkaBroker()
    bus = _wire(broker)

    async def scenario() -> None:
        await bus.start()
        # A poison record straight onto the topic, behind the bus's own codec;
        # same key as the valid publish below, so it sits at the lower offset on
        # the shared partition and the poll loop reaches it first.
        broker.produce(key=b"BTC", value=b"not a valid envelope")
        with pytest.raises(msgspec.DecodeError):
            await bus.publish(_tick(symbol="BTC"))  # drains, and re-raises the decode fault
        await bus.close()

    # Bounded: the old behaviour (decode outside the try) would hang here.
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
    exchange = PaperExchange(
        bus=bus,
        clock=clock,
        fill_model=ImmediateFillModel(),
        genesis_collateral=GENESIS,
        account_net=dict,
    )
    manager = ExecutionManager(
        bus=bus, exchange=exchange, checkpointer=checkpointer(store, clock=clock)
    )
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
