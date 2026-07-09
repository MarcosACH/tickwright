"""``KafkaBus`` — the ADR-0023 topology over one keyed topic (ADR-0028).

Same observable contract as ``InMemoryBus`` — at-least-once delivery,
per-symbol ordering only, pub/sub only — with durability and replay under it.
One ``tickwright.events`` topic, records keyed by ``event.partition_key`` so a
symbol's whole causal chain lands on one partition, a single consumer group
(no competing consumers, ADR-0001). The wire format lives in ``serde``; the
``aiokafka`` client classes are injected as factories so tests can stand a
fake at the exact process boundary (ADR-0022).

The poll loop is the delivery half of the parity story: it dispatches one
decoded record at a time to every matching subscriber — the same inner loop as
``InMemoryBus``'s drain — and commits the offset only *after* the handlers
finished, so a crash mid-dispatch redelivers (at-least-once) instead of losing
the event. A handler publishing reentrantly just produces a later record: the
poll loop reaches it after the current cascade generation, mirroring the
in-memory FIFO's breadth-first order.
"""

import asyncio
from collections.abc import Callable
from typing import Protocol

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

from tickwright.domain import Event
from tickwright.domain.protocols import Handler

from .serde import decode_event, encode_event


class _ProducerLike(Protocol):
    """The slice of the aiokafka producer surface the bus stands on."""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send_and_wait(self, topic: str, value: bytes, key: bytes) -> object: ...


class _ConsumedRecord(Protocol):
    """The slice of one consumed message the bus reads."""

    @property
    def value(self) -> bytes: ...


class _ConsumerLike(Protocol):
    """The slice of the aiokafka consumer surface the bus stands on."""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def getone(self) -> _ConsumedRecord: ...
    async def commit(self) -> None: ...


class KafkaBus:
    """An ``EventBus`` whose transport is one keyed, durable Kafka topic."""

    def __init__(
        self,
        *,
        bootstrap_servers: str,
        topic: str,
        group_id: str,
        producer_factory: Callable[..., _ProducerLike] = AIOKafkaProducer,
        consumer_factory: Callable[..., _ConsumerLike] = AIOKafkaConsumer,
    ) -> None:
        self._bootstrap_servers = bootstrap_servers
        self._topic = topic
        self._group_id = group_id
        self._producer_factory = producer_factory
        self._consumer_factory = consumer_factory
        self._producer: _ProducerLike | None = None
        self._consumer: _ConsumerLike | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._subscriptions: list[tuple[type[Event], Handler[Event]]] = []

    def subscribe[E: Event](self, event_type: type[E], handler: Handler[E]) -> None:
        # Same storage-and-guard shape as InMemoryBus: stored as Handler[Event],
        # dispatch guards with isinstance so a handler only sees its own type.
        self._subscriptions.append((event_type, handler))  # type: ignore[arg-type]

    async def start(self) -> None:
        """Connect the producer and consumer, then start the poll loop."""
        self._producer = self._producer_factory(bootstrap_servers=self._bootstrap_servers)
        await self._producer.start()
        self._consumer = self._consumer_factory(
            self._topic,
            bootstrap_servers=self._bootstrap_servers,
            group_id=self._group_id,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
        )
        await self._consumer.start()
        self._poll_task = asyncio.create_task(self._poll(self._consumer))

    async def publish(self, event: Event) -> None:
        if self._producer is None:
            raise RuntimeError("KafkaBus.publish before start()")
        await self._producer.send_and_wait(
            self._topic, value=encode_event(event), key=event.partition_key.encode()
        )

    async def close(self) -> None:
        """Stop consuming, then flush and disconnect — buffered writes survive."""
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        if self._consumer is not None:
            await self._consumer.stop()
            self._consumer = None
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None

    async def _poll(self, consumer: _ConsumerLike) -> None:
        """Deliver one record at a time; commit only after its handlers ran."""
        while True:
            record = await consumer.getone()
            event = decode_event(record.value)
            for event_type, handler in list(self._subscriptions):
                if isinstance(event, event_type):
                    await handler(event)
            await consumer.commit()
