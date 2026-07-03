"""Property suite: the order FSM admits only the ADR-0007/0010 transitions.

The spec table here is transcribed from the ADRs — deliberately not imported
from the implementation — so the property pins the documented FSM: whatever
event sequence Hypothesis throws at an ``Order``, every accepted transition is
in the table, every out-of-table attempt raises ``InvariantViolation`` and
leaves the saga untouched, and a full replay of any delivery converges.
"""

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tickwright.domain import (
    InvariantViolation,
    Order,
    OrderCancelled,
    OrderDenied,
    OrderEvent,
    OrderFailed,
    OrderFilled,
    OrderLive,
    OrderPartiallyFilled,
    OrderPlaced,
    OrderRejected,
    OrderState,
    OrderSubmitted,
    OrderType,
    Side,
)

# The legal (from, to) pairs per ADR-0007 as refined by ADR-0010 — the spec.
_SPEC_TRANSITIONS: frozenset[tuple[OrderState, OrderState]] = frozenset(
    {
        (OrderState.PENDING, OrderState.SUBMITTED),
        (OrderState.PENDING, OrderState.DENIED),
        (OrderState.SUBMITTED, OrderState.LIVE),
        (OrderState.SUBMITTED, OrderState.PARTIALLY_FILLED),
        (OrderState.SUBMITTED, OrderState.FILLED),
        (OrderState.SUBMITTED, OrderState.CANCELLED),
        (OrderState.SUBMITTED, OrderState.REJECTED),
        (OrderState.SUBMITTED, OrderState.FAILED),
        (OrderState.LIVE, OrderState.PARTIALLY_FILLED),
        (OrderState.LIVE, OrderState.FILLED),
        (OrderState.LIVE, OrderState.CANCELLED),
        (OrderState.LIVE, OrderState.REJECTED),
        (OrderState.PARTIALLY_FILLED, OrderState.PARTIALLY_FILLED),
        (OrderState.PARTIALLY_FILLED, OrderState.FILLED),
        (OrderState.PARTIALLY_FILLED, OrderState.CANCELLED),
    }
)

_IDENTITY = {
    "cloid": "0xabc",
    "strategy_id": "trivial",
    "signal_id": "trivial:BTC:1",
    "symbol": "BTC",
}


def _order() -> Order:
    return Order(
        cloid="0xabc",
        strategy_id="trivial",
        signal_id="trivial:BTC:1",
        symbol="BTC",
        side=Side.BUY,
        quantity=Decimal("2"),
        order_type=OrderType.MARKET,
    )


_IDENTITY_STRATEGIES = {key: st.just(value) for key, value in _IDENTITY.items()}


def _plain(event_type: type[OrderEvent]) -> st.SearchStrategy[OrderEvent]:
    return st.builds(event_type, ts_event=st.just(1), ts_init=st.just(1), **_IDENTITY_STRATEGIES)


def _reasoned(event_type: type[OrderEvent]) -> st.SearchStrategy[OrderEvent]:
    return st.builds(
        event_type,
        ts_event=st.just(1),
        ts_init=st.just(1),
        reason=st.sampled_from(["guard", "venue said no", "hard failure"]),
        **_IDENTITY_STRATEGIES,
    )


def _fill(event_type: type[OrderEvent]) -> st.SearchStrategy[OrderEvent]:
    # A small trade_id alphabet so generated sequences contain duplicates.
    return st.builds(
        event_type,
        ts_event=st.just(1),
        ts_init=st.just(1),
        trade_id=st.sampled_from(["t1", "t2", "t3"]),
        quantity=st.just(Decimal("1")),
        price=st.just(Decimal("100")),
        cum_qty=st.just(Decimal("1")),
        **_IDENTITY_STRATEGIES,
    )


_ANY_ORDER_EVENT: st.SearchStrategy[OrderEvent] = st.one_of(
    _plain(OrderPlaced),
    _plain(OrderSubmitted),
    _plain(OrderLive),
    _plain(OrderCancelled),
    _reasoned(OrderDenied),
    _reasoned(OrderRejected),
    _reasoned(OrderFailed),
    _fill(OrderPartiallyFilled),
    _fill(OrderFilled),
)

_EVENT_SEQUENCES = st.lists(_ANY_ORDER_EVENT, min_size=1, max_size=12)


@given(events=_EVENT_SEQUENCES)
def test_no_event_sequence_can_drive_an_illegal_transition(events: list[OrderEvent]) -> None:
    order = _order()
    for event in events:
        before = order.state
        try:
            advanced = order.apply(event)
        except InvariantViolation:
            # Refused: the attempt must be out-of-spec and the saga untouched.
            assert (before, event.state) not in _SPEC_TRANSITIONS
            assert order.state is before
        else:
            if advanced:
                # Accepted: the transition must be in the spec table.
                assert (before, event.state) in _SPEC_TRANSITIONS
                assert order.state is event.state
            else:
                # Deduped redelivery: a no-op never moves the saga.
                assert order.state is before


@given(events=_EVENT_SEQUENCES)
def test_replaying_every_delivered_event_converges(events: list[OrderEvent]) -> None:
    order = _order()
    delivered: list[OrderEvent] = []
    for event in events:
        try:
            order.apply(event)
        except InvariantViolation:
            continue
        delivered.append(event)

    state, cum_qty, reason = order.state, order.cum_qty, order.reason
    # At-least-once redelivery of the whole history is a no-op end to end.
    for event in delivered:
        assert order.apply(event) is False
    assert order.state is state
    assert order.cum_qty == cum_qty
    assert order.reason == reason


def test_illegal_apply_raises_invariant_violation_and_preserves_state() -> None:
    order = _order()
    order.apply(
        OrderDenied(ts_event=1, ts_init=1, reason="guard", **_IDENTITY)  # type: ignore[arg-type]
    )
    with pytest.raises(InvariantViolation):
        order.apply(OrderSubmitted(ts_event=2, ts_init=2, **_IDENTITY))  # type: ignore[arg-type]
    assert order.state is OrderState.DENIED
