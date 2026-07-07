"""Correlation ids are ambient, not arguments (#18, ADR-0020).

A per-process ``run_id`` and a per-operation id (``cloid`` in saga handling,
``signal_id`` in signal handling, ``cycle`` in reconciliation) live in
``ContextVar``s and are merged into every record by the processor chain — the
call site never repeats them as fields. ``capture_events`` observes the merged
record so a test can assert what a real log line would carry.
"""

from tickwright.observability import NamedEvent, named_event
from tickwright.observability.correlation import bind_run_id, operation
from tickwright.observability.testing import capture_events


def test_the_run_id_rides_every_record_once_bound() -> None:
    bind_run_id("run-1")
    with capture_events() as events:
        named_event(NamedEvent.ORDER_FILLED)

    assert events[0]["run_id"] == "run-1"


def test_an_operation_id_is_bound_for_its_scope_and_cleared_after() -> None:
    with capture_events() as events:
        with operation(cloid="0xabc"):
            named_event(NamedEvent.ORDER_FILLED)
        named_event(NamedEvent.ORDER_CANCELLED)

    assert events[0]["event"] == "order.filled"
    assert events[0]["cloid"] == "0xabc"
    # Left the scope: the second record carries no cloid.
    assert events[1]["event"] == "order.cancelled"
    assert "cloid" not in events[1]


def test_operations_nest_so_a_cycle_and_a_cloid_can_both_ride_a_record() -> None:
    with capture_events() as events:
        with operation(cycle="startup"), operation(cloid="0xabc"):
            named_event(NamedEvent.RECONCILE_FROZEN)

    assert events[0]["cycle"] == "startup"
    assert events[0]["cloid"] == "0xabc"
