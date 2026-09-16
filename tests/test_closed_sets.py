"""``assert_covers_exactly`` bites in both directions and names the side (#303).

The helper is the one place the "a hand map covers a closed set exactly"
rule is written. Each case here is one way that rule could go quiet: a
missing row, a stale row, a closed set read the wrong way. The messages are
asserted because this is what a stale map fails with, and a bare set
comparison was what two of the five callers had before.
"""

from enum import Enum
from typing import Literal, Protocol

import pytest
from closed_sets import assert_covers_exactly


class _Colour(Enum):
    RED = "red"
    BLUE = "blue"


class _Seam(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...


def test_a_map_over_every_member_is_green() -> None:
    assert_covers_exactly(_Colour, {_Colour.RED: 1, _Colour.BLUE: 2}, what="the map")


def test_a_member_with_no_row_fails_naming_the_member_and_the_map() -> None:
    with pytest.raises(AssertionError, match=r"_Colour.*no row in the map.*BLUE"):
        assert_covers_exactly(_Colour, {_Colour.RED: 1}, what="the map")


def test_a_row_for_a_member_the_set_no_longer_has_fails_naming_the_row() -> None:
    with pytest.raises(AssertionError, match=r"rows in the map.*_Colour no longer has.*'green'"):
        assert_covers_exactly(
            _Colour, {_Colour.RED: 1, _Colour.BLUE: 2, "green": 3}, what="the map"
        )


def test_a_hint_rides_on_the_missing_row_message() -> None:
    with pytest.raises(AssertionError, match=r"BLUE.*add the test"):
        assert_covers_exactly(_Colour, {_Colour.RED: 1}, what="the map", hint="add the test")


def test_a_protocol_is_read_by_member_name() -> None:
    assert_covers_exactly(_Seam, {"start": 1, "stop": 2}, what="the map")
    with pytest.raises(AssertionError, match=r"'stop'"):
        assert_covers_exactly(_Seam, {"start": 1}, what="the map")


def test_a_literal_is_read_by_value() -> None:
    assert_covers_exactly(Literal["a", "b"], ["a", "b"], what="the list")
    with pytest.raises(AssertionError, match=r"'b'"):
        assert_covers_exactly(Literal["a", "b"], ["a"], what="the list")


def test_an_excluded_member_is_owed_no_row_and_may_not_have_one() -> None:
    assert_covers_exactly(_Seam, {"start": 1}, what="the map", excluding={"stop"})
    with pytest.raises(AssertionError, match=r"'stop'"):
        assert_covers_exactly(_Seam, {"start": 1, "stop": 2}, what="the map", excluding={"stop"})
