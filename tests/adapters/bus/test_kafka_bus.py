"""``KafkaBus`` — the same ADR-0023 topology over one keyed topic (ADR-0028).

The Kafka client is faked at the process boundary (ADR-0022): the fakes stand
in for ``aiokafka``'s producer/consumer classes and record what crossed the
wire; everything on our side of that line — serde, keying, dispatch — is real.
"""

import asyncio
from decimal import Decimal

from tickwright.adapters.bus.kafka import KafkaBus
from tickwright.adapters.bus.serde import decode_event
from tickwright.domain import MarketTick
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


class FakeProducer:
    """The aiokafka producer boundary: records every sent record."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes, bytes]] = []
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def send_and_wait(self, topic: str, value: bytes, key: bytes) -> None:
        assert self.started, "send before producer.start()"
        self.sent.append((topic, key, value))


def test_publish_sends_one_encoded_record_keyed_by_partition_key() -> None:
    producer = FakeProducer()
    bus = KafkaBus(
        bootstrap_servers="kafka:9092",
        topic="tickwright.events",
        group_id="tickwright",
        producer_factory=lambda **_: producer,
    )
    tick = _tick(symbol="ETH")

    async def scenario() -> None:
        await bus.start()
        await bus.publish(tick)

    asyncio.run(scenario())

    assert len(producer.sent) == 1
    topic, key, value = producer.sent[0]
    assert topic == "tickwright.events"
    assert key == b"ETH"
    assert decode_event(value) == tick
