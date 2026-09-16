"""One hypothesis strategy that builds any publishable event family.

Two gates walk ``publishable_event_types()`` and need a real instance of each
family, not the class: the serde round trip and the domain key gate. Both read
the field types off the dataclass, so a new family is covered the day it
exists, with no per-family fixture to forget.

Importable as a top-level module (``pythonpath = ["tests/_support"]``), so
``from event_instances import instances``.
"""

import dataclasses
import enum
import types
import typing
from decimal import Decimal

from hypothesis import strategies as st

from tickwright.domain import Event

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


def instances(cls: type[Event]) -> st.SearchStrategy[Event]:
    """Build instances of one event family from its dataclass field types."""
    hints = typing.get_type_hints(cls)
    return st.builds(
        cls,
        **{field.name: _strategy_for_field(hints[field.name]) for field in dataclasses.fields(cls)},
    )
