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

from .drain_ledger import DrainLedger
from .serde import decode_event, encode_event
from .subscriptions import Subscriptions


class _RecordCoordinates(Protocol):
    """Where a record landed: aiokafka's ``RecordMetadata`` / consumed-message slice."""

    @property
    def partition(self) -> int: ...
    @property
    def offset(self) -> int: ...


class _ProducerLike(Protocol):
    """The slice of the aiokafka producer surface the bus stands on."""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send_and_wait(self, topic: str, value: bytes, key: bytes) -> _RecordCoordinates: ...


class _ConsumedRecord(_RecordCoordinates, Protocol):
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
        self._subscriptions = Subscriptions()
        # The drain fence: produced-vs-committed offsets per partition. Single
        # process, single group (ADR-0001) — we are the only producer, so "our
        # records are committed" is exactly "the topic went idle".
        self._ledger = DrainLedger()
        self._dispatch_fault: BaseException | None = None

    def subscribe[E: Event](self, event_type: type[E], handler: Handler[E]) -> None:
        self._subscriptions.subscribe(event_type, handler)

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
        placed = await self._producer.send_and_wait(
            self._topic, value=encode_event(event), key=event.partition_key.encode()
        )
        self._ledger.record_produced(placed.partition, placed.offset)
        # The parity trick (ADR-0023): a top-level publish returns only after
        # the whole cascade it began was delivered — exactly the in-memory
        # drain-to-quiescence contract, so callers observe one behavior on
        # both backends (and a handler exception surfaces *here*, in the
        # publish that caused it). A publish from inside a handler runs in
        # the poll task: it must enqueue-and-unwind (draining would deadlock
        # the dispatcher on itself), mirroring the in-memory reentrant FIFO.
        if asyncio.current_task() is not self._poll_task:
            await self.drain()

    async def drain(self) -> None:
        """Wait until every record this process produced was dispatched and committed.

        A handler publishing mid-drain raises the high-water mark, so the wait
        naturally extends to the whole reentrant cascade — the Kafka face of
        the in-memory drain-to-quiescence contract (ADR-0023/0024).
        """
        while True:
            self._reraise_dispatch_fault()
            if not self._ledger.in_flight():
                return
            await self._ledger.wait_for_progress()

    def _reraise_dispatch_fault(self) -> None:
        """Containment parity (ADR-0024): on the in-memory bus a raw handler
        exception propagates into the publish that caused it — raised once,
        and the bus survives. Here dispatch runs in the poll loop, so the
        fault is handed to the earliest drain (normally the publish whose
        cascade broke, since a top-level publish drains) and *popped*: the
        next publish works again, matching the in-memory contract."""
        if self._dispatch_fault is not None:
            fault, self._dispatch_fault = self._dispatch_fault, None
            raise fault

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
            try:
                await self._subscriptions.dispatch(event)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                # Stored for the draining publisher to re-raise (containment
                # parity), then keep serving — the in-memory bus survives a
                # handler fault too. The record is *not* committed: within
                # this session the fetch position already moved past it, but
                # a restart redelivers it (at-least-once, offsets bound it).
                # Local accounting still advances so a later drain terminates
                # instead of waiting forever on the record we just dropped.
                self._dispatch_fault = exc
                self._ledger.record_committed(record.partition, record.offset)
                continue
            await consumer.commit()
            self._ledger.record_committed(record.partition, record.offset)
