"""The Kafka cluster behind the process boundary (ADR-0022), shared by suites.

``FakeKafkaBroker`` stands in for the cluster behind aiokafka's
producer/consumer surface, with real partitioning by key hash and
per-partition offsets; everything on our side of that line — serde, keying,
dispatch, commits — is real ``KafkaBus`` code.
"""

import asyncio
import zlib
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Record:
    """What one produced/consumed message looks like on the aiokafka surface."""

    value: bytes
    key: bytes
    partition: int
    offset: int


@dataclass
class FakeKafkaBroker:
    """Keyed partitions, offsets, commits — the two guarantees the bus stands on.

    Per-partition append order is preserved and records with the same key land
    on the same partition. Everything else (network, rebalancing, multiple
    groups) is out of scope by ADR-0001/0028: one process, one consumer group.
    """

    partition_count: int = 3
    partitions: list[list[tuple[bytes, bytes]]] = field(default_factory=list)
    committed: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.partitions = [[] for _ in range(self.partition_count)]
        self.committed = [0] * self.partition_count
        self._changed: asyncio.Event = asyncio.Event()
        # Every client handed out, so suites can observe lifecycle at the
        # boundary: connected during a run, disconnected after teardown.
        self.producers: list[FakeProducer] = []
        self.consumers: list[FakeConsumer] = []

    def partition_for(self, key: bytes) -> int:
        return zlib.crc32(key) % self.partition_count

    def produce(self, key: bytes, value: bytes) -> Record:
        partition = self.partition_for(key)
        self.partitions[partition].append((key, value))
        self._notify()
        offset = len(self.partitions[partition]) - 1
        return Record(value=value, key=key, partition=partition, offset=offset)

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
        self.producers.append(FakeProducer(self))
        return self.producers[-1]

    def consumer(self, *_: object, **__: object) -> "FakeConsumer":
        self.consumers.append(FakeConsumer(self))
        return self.consumers[-1]


class FakeProducer:
    """The aiokafka producer boundary: appends to the broker's partitions."""

    def __init__(self, broker: FakeKafkaBroker) -> None:
        self._broker = broker
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def send_and_wait(self, topic: str, value: bytes, key: bytes) -> Record:
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

    async def getone(self) -> Record:
        assert self.started, "getone before consumer.start()"
        while True:
            for step in range(self._broker.partition_count):
                p = (self._next_partition + step) % self._broker.partition_count
                if self._positions[p] < len(self._broker.partitions[p]):
                    offset = self._positions[p]
                    key, value = self._broker.partitions[p][offset]
                    self._positions[p] = offset + 1
                    self._next_partition = (p + 1) % self._broker.partition_count
                    return Record(value=value, key=key, partition=p, offset=offset)
            await self._broker._wait_for_change()

    async def commit(self) -> None:
        self._broker.committed = list(self._positions)
        self._broker._notify()
