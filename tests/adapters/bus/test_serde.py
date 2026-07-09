"""The serde codec at the Kafka edge (ADR-0025).

Serialization is a boundary concern: domain events stay stdlib frozen
dataclasses, and only ``KafkaBus`` touches a wire format. The codec's whole
contract is lossless round-tripping — ``decode(encode(event))`` yields an
*equal object of the same concrete class*, so a fill that crossed Kafka is
indistinguishable from one that never left the process.
"""

import dataclasses
import enum
import types
import typing
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from tickwright.adapters.bus.serde import decode_event, encode_event
from tickwright.domain import Event, MarketTick, events
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


# ---- Property: every publishable event family round-trips -------------------
#
# The families are *derived* from the domain events module (the leaves of the
# subclass tree are exactly the publishable concrete classes; interior nodes
# are bases with abstract key derivations), so a new event family the codec
# cannot handle fails here — the codec can never silently lag the schema.
# Derivation reads the module namespace, not ``__subclasses__()``, because
# ``@dataclass(slots=True)`` recreates each class and leaves stale pre-slots
# originals in ``__subclasses__()``.


def _publishable_event_types() -> list[type[Event]]:
    classes = [
        obj
        for obj in vars(events).values()
        if isinstance(obj, type) and issubclass(obj, Event) and obj is not Event
    ]
    bases = {base for cls in classes for base in cls.__bases__}
    return sorted((cls for cls in classes if cls not in bases), key=lambda cls: cls.__name__)


_WIRE_TEXT = st.text(min_size=1, max_size=20)
_DECIMALS = st.decimals(allow_nan=False, allow_infinity=False)


def _strategy_for_field(field_type: object) -> st.SearchStrategy[object]:
    if isinstance(field_type, types.UnionType):  # e.g. `str | None`
        members = typing.get_args(field_type)
        return st.one_of(
            *(_strategy_for_field(member) for member in members if member is not types.NoneType),
            st.none(),
        )
    assert isinstance(field_type, type)
    if issubclass(field_type, enum.Enum):
        return st.sampled_from(field_type)
    if field_type is bool:
        return st.booleans()
    if field_type is int:
        return st.integers(min_value=0, max_value=2**63 - 1)
    if field_type is str:
        return _WIRE_TEXT
    if field_type is Decimal:
        return _DECIMALS
    raise NotImplementedError(f"no strategy for field type {field_type!r}")


def _instances(cls: type[Event]) -> st.SearchStrategy[Event]:
    hints = typing.get_type_hints(cls)
    return st.builds(
        cls,
        **{field.name: _strategy_for_field(hints[field.name]) for field in dataclasses.fields(cls)},
    )


@given(event=st.one_of([_instances(cls) for cls in _publishable_event_types()]))
def test_every_event_family_round_trips_to_an_equal_object(event: Event) -> None:
    restored = decode_event(encode_event(event))

    assert restored == event
    assert type(restored) is type(event)
