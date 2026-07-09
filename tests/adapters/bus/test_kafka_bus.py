"""``KafkaBus`` — the same ADR-0023 topology over one keyed topic (ADR-0028).

The Kafka client is faked at the process boundary (ADR-0022): ``FakeKafkaBroker``
stands in for the cluster behind aiokafka's producer/consumer surface, with real
partitioning by key hash and per-partition offsets; everything on our side of
that line — serde, keying, dispatch, commits — is real ``KafkaBus`` code.
"""

import asyncio
import zlib
from dataclasses import dataclass, field
from decimal import Decimal

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


@dataclass(frozen=True, slots=True)
class _Record:
    """What one consumed message looks like off the aiokafka surface."""

    value: bytes
    key: bytes
    partition: int
    offset: int


@dataclass
class FakeKafkaBroker:
    """The cluster behind the process boundary: keyed partitions, offsets, commits.

    Per-partition append order is preserved and records with the same key land
    on the same partition — the two Kafka guarantees the bus's ordering story
    stands on. Everything else (network, rebalancing, multiple groups) is out
    of scope by ADR-0001/0028: one process, one consumer group.
    """

    partition_count: int = 3
    partitions: list[list[tuple[bytes, bytes]]] = field(default_factory=list)
    committed: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.partitions = [[] for _ in range(self.partition_count)]
        self.committed = [0] * self.partition_count
        self._changed: asyncio.Event = asyncio.Event()

    def partition_for(self, key: bytes) -> int:
        return zlib.crc32(key) % self.partition_count

    def produce(self, key: bytes, value: bytes) -> _Record:
        partition = self.partition_for(key)
        self.partitions[partition].append((key, value))
        self._notify()
        offset = len(self.partitions[partition]) - 1
        return _Record(value=value, key=key, partition=partition, offset=offset)

    def _notify(self) -> None:
        self._changed.set()

    async def _wait_for_change(self) -> None:
        self._changed.clear()
        await self._changed.wait()

    async def all_committed(self) -> None:
        """Wait until the consumer group has committed every produced record."""
        while self.committed != [len(p) for p in self.partitions]:
            await self._wait_for_change()

    def producer(self, **_: object) -> "FakeProducer":
        return FakeProducer(self)

    def consumer(self, *_: object, **__: object) -> "FakeConsumer":
        return FakeConsumer(self)


class FakeProducer:
    """The aiokafka producer boundary: appends to the broker's partitions."""

    def __init__(self, broker: FakeKafkaBroker) -> None:
        self._broker = broker
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def send_and_wait(self, topic: str, value: bytes, key: bytes) -> _Record:
        assert self.started, "send before producer.start()"
        return self._broker.produce(key, value)


class FakeConsumer:
    """The aiokafka consumer boundary: per-partition positions, manual commits."""

    def __init__(self, broker: FakeKafkaBroker) -> None:
        self._broker = broker
        self._positions = list(broker.committed)
        self._next_partition = 0
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def getone(self) -> _Record:
        assert self.started, "getone before consumer.start()"
        while True:
            for step in range(self._broker.partition_count):
                p = (self._next_partition + step) % self._broker.partition_count
                if self._positions[p] < len(self._broker.partitions[p]):
                    offset = self._positions[p]
                    key, value = self._broker.partitions[p][offset]
                    self._positions[p] = offset + 1
                    self._next_partition = (p + 1) % self._broker.partition_count
                    return _Record(value=value, key=key, partition=p, offset=offset)
            await self._broker._wait_for_change()

    async def commit(self) -> None:
        self._broker.committed = list(self._positions)
        self._broker._notify()


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
