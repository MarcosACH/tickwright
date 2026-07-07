"""The named-event catalog is one importable, walkable, enforced artifact (#18).

``NamedEvent`` is the single source of truth for every named lifecycle event, and
``named_event`` refuses any name not in it — so "a state-affecting path with no
named event is a defect" has a runtime teeth: an uncataloged name never ships
silently, it raises at the call site (ADR-0020).
"""

import pytest
import structlog.testing

from tickwright.observability import NamedEvent, named_event


def test_cataloged_name_is_emitted_as_a_structured_record() -> None:
    with structlog.testing.capture_logs() as logs:
        named_event(NamedEvent.ORDER_FILLED, quantity="0.5")

    assert [log["event"] for log in logs] == ["order.filled"]
    assert logs[0]["quantity"] == "0.5"


def test_an_uncataloged_name_raises_rather_than_emitting() -> None:
    with pytest.raises(ValueError, match="order.teleported"):
        named_event("order.teleported")


def test_every_catalog_member_is_a_dotted_lowercase_name() -> None:
    # The catalog is walkable: a test can iterate every name it must cover.
    assert len(list(NamedEvent)) > 0
    for event in NamedEvent:
        assert event.value == event.value.lower()
        assert "." in event.value
