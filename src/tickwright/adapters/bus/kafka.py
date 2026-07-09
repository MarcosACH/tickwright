"""``KafkaBus`` — the ADR-0023 topology over one keyed topic (ADR-0028).

Same observable contract as ``InMemoryBus`` — at-least-once delivery,
per-symbol ordering only, pub/sub only — with durability and replay under it.
One ``tickwright.events`` topic, records keyed by ``event.partition_key`` so a
symbol's whole causal chain lands on one partition, a single consumer group
(no competing consumers, ADR-0001). The wire format lives in ``serde``; the
``aiokafka`` client classes are injected as factories so tests can stand a
fake at the exact process boundary (ADR-0022).
"""

from collections.abc import Callable
from typing import Protocol

from aiokafka import AIOKafkaProducer

from tickwright.domain import Event

from .serde import encode_event


class _ProducerLike(Protocol):
    """The slice of the aiokafka producer surface the bus stands on."""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send_and_wait(self, topic: str, value: bytes, key: bytes) -> object: ...


class KafkaBus:
    """An ``EventBus`` whose transport is one keyed, durable Kafka topic."""

    def __init__(
        self,
        *,
        bootstrap_servers: str,
        topic: str,
        group_id: str,
        producer_factory: Callable[..., _ProducerLike] = AIOKafkaProducer,
    ) -> None:
        self._bootstrap_servers = bootstrap_servers
        self._topic = topic
        self._group_id = group_id
        self._producer_factory = producer_factory
        self._producer: _ProducerLike | None = None

    async def start(self) -> None:
        """Connect the producer (ADR-0024 startup step 3)."""
        self._producer = self._producer_factory(bootstrap_servers=self._bootstrap_servers)
        await self._producer.start()

    async def publish(self, event: Event) -> None:
        if self._producer is None:
            raise RuntimeError("KafkaBus.publish before start()")
        await self._producer.send_and_wait(
            self._topic, value=encode_event(event), key=event.partition_key.encode()
        )
