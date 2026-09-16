"""The named-event catalog is one importable, walkable, enforced artifact (#18).

``NamedEvent`` is the single source of truth for every named lifecycle event, and
``named_event`` refuses any name not in it — so "a state-affecting path with no
named event is a defect" has a runtime teeth: an uncataloged name never ships
silently, it raises at the call site (ADR-0020).
"""

import pytest
import structlog.testing

from tickwright.observability import NamedEvent, named_event
from tickwright.observability.catalog import FIELDS


def test_cataloged_name_is_emitted_as_a_structured_record() -> None:
    with structlog.testing.capture_logs() as logs:
        named_event(NamedEvent.ORDER_REJECTED, reason="post_only")

    assert [log["event"] for log in logs] == ["order.rejected"]
    assert logs[0]["reason"] == "post_only"


def test_an_uncataloged_name_raises_rather_than_emitting() -> None:
    with pytest.raises(ValueError, match="order.teleported"):
        named_event("order.teleported")


def test_every_catalog_member_is_a_dotted_lowercase_name() -> None:
    # The catalog is walkable: a test can iterate every name it must cover.
    assert len(list(NamedEvent)) > 0
    for event in NamedEvent:
        assert event.value == event.value.lower()
        assert "." in event.value


def test_every_catalog_member_declares_its_field_set() -> None:
    # The declaration is as closed as the catalog: a name with no field set
    # would be back to the drift #338 removes.
    assert set(FIELDS) == set(NamedEvent)


def test_a_field_set_that_differs_from_the_declared_one_raises_rather_than_emitting() -> None:
    # A declared field set is the other half of the contract (#338): a field
    # whose name or presence varies per call site never ships silently either.
    with structlog.testing.capture_logs() as logs:
        with pytest.raises(ValueError, match="engine.faulted") as excinfo:
            named_event(NamedEvent.ENGINE_FAULTED, reason="boom")

    assert "error" in str(excinfo.value)
    assert "reason" in str(excinfo.value)
    assert logs == []


def test_a_member_with_no_declared_field_set_raises_rather_than_emitting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The runtime check holds the same line the test above draws. A member
    # that slipped past declaration must not fall back to the unchecked emit
    # #338 removed.
    monkeypatch.delitem(FIELDS, NamedEvent.ORDER_PLACED)
    with structlog.testing.capture_logs() as logs:
        with pytest.raises(ValueError, match="order.placed") as excinfo:
            named_event(NamedEvent.ORDER_PLACED)

    # The refusal says what to change, as the other two refusals do.
    assert "FIELDS" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, KeyError)
    assert logs == []
