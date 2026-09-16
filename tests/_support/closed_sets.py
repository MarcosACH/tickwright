"""A hand-written map must cover a closed set exactly, both ways, naming the side.

The engine branches on closed sets: the seam Protocols, the named-event
catalog, ``DivergenceField``, the ``Literal`` discriminants in ``AppConfig``.
Each one has a test-side map keyed by member (a claim, a scenario, a suite, a
backend), and that map is a transcribed list. It goes stale in the one
direction nothing catches: a member added to the set with no row in the map is
a case nothing exercises, and that has happened once (#194). The other
direction is a row for a member the set no longer has, which keeps asserting
something nobody can reach.

One helper so the discipline is written once. Five sites wrote it before
(#303), three naming the stale side in a message and two as a bare
``set(a) == set(b)`` that failed blind.

**Read the way each set is walked.** A Protocol is read by member name, since
the map's keys are the names the tests claim. An ``Enum`` is read by member,
since the map's keys are the members the scenarios drive. A ``Literal`` is
read by value, since the map's keys are what ``AppConfig`` admits.

Explicit assertion messages throughout: this module is not a test module, so
pytest does not rewrite its asserts and a bare comparison would fail blind.
"""

from collections.abc import Collection, Iterable
from enum import Enum
from typing import Literal, get_args, get_origin, get_protocol_members


def assert_covers_exactly(
    closed_set: object,
    rows: Iterable[object],
    *,
    what: str,
    hint: str = "",
    excluding: Collection[object] = (),
) -> None:
    """Assert the keys of ``rows`` are the members of ``closed_set``, no more, no fewer.

    ``closed_set`` is a Protocol, an ``Enum`` or a ``Literal``. ``what`` names
    the map in both messages, so the failure reads as the author's own file.
    ``hint`` rides on the missing-row message, for the one action that fixes
    it. ``excluding`` names members owed no row, and a row for one of them is
    stale like any other.
    """
    members = _members(closed_set) - set(excluding)
    keys = set(rows)
    name = _name(closed_set)
    missing = members - keys
    assert not missing, f"{name} members with no row in {what}: {_listed(missing)}" + (
        f" — {hint}" if hint else ""
    )
    stale = keys - members
    assert not stale, f"rows in {what} for members {name} no longer has: {_listed(stale)}"


def _members(closed_set: object) -> set[object]:
    if get_origin(closed_set) is Literal:
        return set(get_args(closed_set))
    if isinstance(closed_set, type) and issubclass(closed_set, Enum):
        return set(closed_set)
    if isinstance(closed_set, type):
        return set(get_protocol_members(closed_set))
    raise TypeError(f"not a Protocol, Enum or Literal: {closed_set!r}")


def _name(closed_set: object) -> str:
    return closed_set.__name__ if isinstance(closed_set, type) else repr(closed_set)


def _listed(members: set[object]) -> str:
    """Members in a stable order, an ``Enum`` member by its name."""
    return str(sorted(m.name if isinstance(m, Enum) else repr(m) for m in members))
