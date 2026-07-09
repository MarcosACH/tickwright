"""The wire codec at the ``KafkaBus`` edge (ADR-0025).

Serialization is a boundary concern: domain events are stdlib frozen
dataclasses everywhere else, and only this module knows a wire format exists.
The format is a tagged JSON envelope — ``{"type": <concrete class name>,
"data": <fields>}`` — because the consumer dispatches by concrete type
(ADR-0028) and dataclass equality requires the exact class back, not a base.
``msgspec`` does the heavy lifting: ``Decimal`` losslessly as a string, enums
by value, frozen slots dataclasses natively.
"""

import msgspec

from tickwright.domain import Event, MarketTick

_EVENT_TYPES: dict[str, type[Event]] = {
    MarketTick.__name__: MarketTick,
}


class _Envelope(msgspec.Struct):
    type: str
    data: msgspec.Raw


def encode_event(event: Event) -> bytes:
    """Encode ``event`` for the wire, tagged with its concrete class."""
    data = msgspec.Raw(msgspec.json.encode(event))
    return msgspec.json.encode(_Envelope(type=type(event).__name__, data=data))


def decode_event(payload: bytes) -> Event:
    """Decode a wire payload back to an equal object of the same concrete class."""
    envelope = msgspec.json.decode(payload, type=_Envelope)
    return msgspec.json.decode(envelope.data, type=_EVENT_TYPES[envelope.type])
