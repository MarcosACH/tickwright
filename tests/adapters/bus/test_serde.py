"""The serde codec at the Kafka edge (ADR-0025).

Serialization is a boundary concern: domain events stay stdlib frozen
dataclasses, and only ``KafkaBus`` touches a wire format. The codec's whole
contract is lossless round-tripping — ``decode(encode(event))`` yields an
*equal object of the same concrete class*, so a fill that crossed Kafka is
indistinguishable from one that never left the process.
"""

from decimal import Decimal

from tickwright.adapters.bus.serde import decode_event, encode_event
from tickwright.domain import MarketTick
from tickwright.domain.enums import AggressorSide


def test_market_tick_round_trips_to_an_equal_object() -> None:
    tick = MarketTick(
        ts_event=1_700_000_000_000_000_000,
        ts_init=1_700_000_000_000_000_001,
        symbol="BTC",
        price=Decimal("42123.55"),
        size=Decimal("0.007"),
        aggressor_side=AggressorSide.BUY,
        trade_id="t-1",
        seq=7,
    )

    restored = decode_event(encode_event(tick))

    assert restored == tick
    assert type(restored) is MarketTick
