"""``KafkaBus`` — the same ADR-0023 topology over one keyed topic (ADR-0028).

The Kafka client is faked at the process boundary (ADR-0022): ``FakeKafkaBroker``
stands in for the cluster behind aiokafka's producer/consumer surface, with real
partitioning by key hash and per-partition offsets; everything on our side of
that line — serde, keying, dispatch, commits — is real ``KafkaBus`` code.
"""

import asyncio
from decimal import Decimal

import pytest
from kafka_fakes import FakeKafkaBroker

from tickwright.adapters.bus.kafka import KafkaBus
from tickwright.adapters.bus.serde import decode_event
from tickwright.domain import Event, EventBus, MarketTick
from tickwright.domain.enums import AggressorSide


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


def test_a_handler_fault_resurfaces_on_the_next_publish_and_on_drain() -> None:
    # Containment parity (ADR-0024): on InMemoryBus a raw handler exception
    # propagates into the publish that caused it. Over Kafka dispatch happens
    # in the poll loop, so the fault is stored and re-raised at the next
    # hot-path touch — the feed's next publish faults the engine, and a
    # shutdown drain cannot wait forever on a dispatcher that died.
    broker = FakeKafkaBroker()
    bus = _wire(broker)

    async def broken(event: MarketTick) -> None:
        raise RuntimeError("handler broke an engine assumption")

    bus.subscribe(MarketTick, broken)

    async def scenario() -> None:
        await bus.start()
        await bus.publish(_tick(1))
        with pytest.raises(RuntimeError, match="handler broke"):
            await bus.drain()
        with pytest.raises(RuntimeError, match="handler broke"):
            await bus.publish(_tick(2))
        await bus.close()  # teardown still works on a faulted bus

    # Bounded: a wrong implementation must fail loudly, not hang in drain.
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
