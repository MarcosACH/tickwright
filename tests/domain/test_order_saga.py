"""The ``Order`` saga record and its idempotent ``apply`` (ADR-0007 / ADR-0025).

``apply`` is the single authority for transition legality and dedup: it is a
no-op on an already-reflected ``event_id`` (so duplicate delivery converges) and
raises ``InvariantViolation`` on an illegal transition (fail-fast, ADR-0014). The
tracer exercises the happy-path subset ``PENDING → SUBMITTED → FILLED``.
"""

from decimal import Decimal

import pytest

from tickwright.domain import (
    InvariantViolation,
    Order,
    OrderFilled,
    OrderState,
    OrderSubmitted,
    OrderType,
    Side,
)


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


def _submitted() -> OrderSubmitted:
    return OrderSubmitted(
        ts_event=1,
        ts_init=1,
        cloid="0xabc",
        strategy_id="trivial",
        signal_id="trivial:BTC:1",
        symbol="BTC",
        venue_oid="oid-1",
    )


def _filled(trade_id: str = "v1", cum_qty: str = "2") -> OrderFilled:
    return OrderFilled(
        ts_event=2,
        ts_init=2,
        cloid="0xabc",
        strategy_id="trivial",
        signal_id="trivial:BTC:1",
        symbol="BTC",
        trade_id=trade_id,
        quantity=Decimal("2"),
        price=Decimal("100"),
        cum_qty=Decimal(cum_qty),
    )


def test_new_order_starts_pending() -> None:
    order = _order()
    assert order.state is OrderState.PENDING
    assert order.cum_qty == Decimal("0")


def test_apply_submitted_advances_to_submitted_and_records_venue_oid() -> None:
    order = _order()
    order.apply(_submitted())
    assert order.state is OrderState.SUBMITTED
    assert order.venue_oid == "oid-1"


def test_apply_filled_after_submitted_sets_filled_and_cum_qty() -> None:
    order = _order()
    order.apply(_submitted())
    order.apply(_filled())
    assert order.state is OrderState.FILLED
    assert order.cum_qty == Decimal("2")


def test_apply_is_idempotent_on_a_reapplied_event() -> None:
    order = _order()
    order.apply(_submitted())
    order.apply(_filled())
    # A redelivered fill (same trade_id -> same event_id) must not double-count.
    order.apply(_filled())
    assert order.state is OrderState.FILLED
    assert order.cum_qty == Decimal("2")


def test_illegal_transition_raises_invariant_violation() -> None:
    order = _order()
    # A fill on a still-PENDING order (never submitted) is not a legal entry.
    with pytest.raises(InvariantViolation):
        order.apply(_filled())
