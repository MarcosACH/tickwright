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

from tickwright.domain import Event, events


def _publishable_event_types() -> dict[str, type[Event]]:
    """The leaves of the ``Event`` subclass tree, keyed by class name.

    The leaves are exactly the publishable concrete families; interior nodes
    (``Signal``, ``ExecutionReport``, ``OrderEvent``, …) are bases with
    abstract key derivations. Deriving the registry keeps the codec incapable
    of lagging the schema: a new family is encodable the day it exists.

    Derived from the events module namespace, not ``__subclasses__()``:
    ``@dataclass(slots=True)`` *recreates* each class, and the abandoned
    pre-slots originals linger in ``__subclasses__()`` until GC — the module
    namespace and ``__bases__`` only ever hold the final class objects.
    """
    classes = [
        obj
        for obj in vars(events).values()
        if isinstance(obj, type) and issubclass(obj, Event) and obj is not Event
    ]
    bases = {base for cls in classes for base in cls.__bases__}
    return {cls.__name__: cls for cls in classes if cls not in bases}


_EVENT_TYPES: dict[str, type[Event]] = _publishable_event_types()


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
