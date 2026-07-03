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
    OrderCancelled,
    OrderDenied,
    OrderFailed,
    OrderFilled,
    OrderLive,
    OrderPartiallyFilled,
    OrderRejected,
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


def _live() -> OrderLive:
    return OrderLive(
        ts_event=2,
        ts_init=2,
        cloid="0xabc",
        strategy_id="trivial",
        signal_id="trivial:BTC:1",
        symbol="BTC",
        venue_oid="oid-1",
    )


def _partial(trade_id: str = "v1", quantity: str = "1", cum_qty: str = "1") -> OrderPartiallyFilled:
    return OrderPartiallyFilled(
        ts_event=2,
        ts_init=2,
        cloid="0xabc",
        strategy_id="trivial",
        signal_id="trivial:BTC:1",
        symbol="BTC",
        trade_id=trade_id,
        quantity=Decimal(quantity),
        price=Decimal("100"),
        cum_qty=Decimal(cum_qty),
    )


def test_apply_partial_fill_after_submitted_records_cum_qty() -> None:
    order = _order()
    order.apply(_submitted())
    order.apply(_partial(trade_id="v1", quantity="1", cum_qty="1"))
    assert order.state is OrderState.PARTIALLY_FILLED
    assert order.cum_qty == Decimal("1")


def test_repeated_partial_fills_self_loop_and_accumulate() -> None:
    order = _order()
    order.apply(_submitted())
    order.apply(_partial(trade_id="v1", quantity="1", cum_qty="1"))
    order.apply(_partial(trade_id="v2", quantity="0.5", cum_qty="1.5"))
    assert order.state is OrderState.PARTIALLY_FILLED
    assert order.cum_qty == Decimal("1.5")


def test_duplicated_partial_fill_same_trade_id_never_double_counts() -> None:
    order = _order()
    order.apply(_submitted())
    order.apply(_partial(trade_id="v1", quantity="1", cum_qty="1"))
    # Redelivered: same trade_id -> same event_id -> no-op, cum_qty unchanged.
    assert order.apply(_partial(trade_id="v1", quantity="1", cum_qty="2")) is False
    assert order.cum_qty == Decimal("1")


def test_apply_live_after_submitted_advances_to_live() -> None:
    order = _order()
    order.apply(_submitted())
    order.apply(_live())
    assert order.state is OrderState.LIVE


def test_live_order_fills_through_partial_to_filled() -> None:
    order = _order()
    order.apply(_submitted())
    order.apply(_live())
    order.apply(_partial(trade_id="v1", quantity="1", cum_qty="1"))
    order.apply(_filled(trade_id="v2", cum_qty="2"))
    assert order.state is OrderState.FILLED
    assert order.cum_qty == Decimal("2")


def test_live_order_fills_fully_in_one_report() -> None:
    order = _order()
    order.apply(_submitted())
    order.apply(_live())
    order.apply(_filled())
    assert order.state is OrderState.FILLED


def test_guard_denied_order_terminates_with_reason_before_any_send() -> None:
    order = _order()
    order.apply(
        OrderDenied(
            ts_event=1,
            ts_init=1,
            cloid="0xabc",
            strategy_id="trivial",
            signal_id="trivial:BTC:1",
            symbol="BTC",
            reason="below min notional",
        )
    )
    assert order.state is OrderState.DENIED
    assert order.reason == "below min notional"


def test_venue_rejection_terminates_a_submitted_order_with_reason() -> None:
    order = _order()
    order.apply(_submitted())
    order.apply(
        OrderRejected(
            ts_event=2,
            ts_init=2,
            cloid="0xabc",
            strategy_id="trivial",
            signal_id="trivial:BTC:1",
            symbol="BTC",
            reason="perp not listed",
        )
    )
    assert order.state is OrderState.REJECTED
    assert order.reason == "perp not listed"


def test_proven_non_landing_fails_a_submitted_order_with_reason() -> None:
    order = _order()
    order.apply(_submitted())
    order.apply(
        OrderFailed(
            ts_event=2,
            ts_init=2,
            cloid="0xabc",
            strategy_id="trivial",
            signal_id="trivial:BTC:1",
            symbol="BTC",
            reason="connection refused before send",
        )
    )
    assert order.state is OrderState.FAILED
    assert order.reason == "connection refused before send"


def _cancelled() -> OrderCancelled:
    return OrderCancelled(
        ts_event=3,
        ts_init=3,
        cloid="0xabc",
        strategy_id="trivial",
        signal_id="trivial:BTC:1",
        symbol="BTC",
    )


def _rejected(reason: str = "venue said no") -> OrderRejected:
    return OrderRejected(
        ts_event=3,
        ts_init=3,
        cloid="0xabc",
        strategy_id="trivial",
        signal_id="trivial:BTC:1",
        symbol="BTC",
        reason=reason,
    )


def test_live_order_can_be_cancelled() -> None:
    order = _order()
    order.apply(_submitted())
    order.apply(_live())
    order.apply(_cancelled())
    assert order.state is OrderState.CANCELLED


def test_ghost_live_order_with_no_fills_resolves_to_rejected() -> None:
    order = _order()
    order.apply(_submitted())
    order.apply(_live())
    order.apply(_rejected(reason="vanished with no fills recorded"))
    assert order.state is OrderState.REJECTED


def test_ghost_partially_filled_order_cancels_with_fills_preserved() -> None:
    order = _order()
    order.apply(_submitted())
    order.apply(_partial(trade_id="v1", quantity="1", cum_qty="1"))
    order.apply(_cancelled())
    assert order.state is OrderState.CANCELLED
    assert order.cum_qty == Decimal("1")  # recorded fills preserved (ADR-0010)


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


# --- record_fill: Order owns fill accounting (ADR-0007) ---------------------


def test_record_fill_for_the_full_quantity_returns_order_filled() -> None:
    order = _order()
    order.apply(_submitted())
    event = order.record_fill(
        trade_id="v1", quantity=Decimal("2"), price=Decimal("100"), ts_event=2, ts_init=2
    )
    assert isinstance(event, OrderFilled)
    assert order.state is OrderState.FILLED
    assert order.cum_qty == Decimal("2")


def test_record_fill_leaving_quantity_working_returns_partially_filled() -> None:
    order = _order()
    order.apply(_submitted())
    event = order.record_fill(
        trade_id="v1", quantity=Decimal("0.5"), price=Decimal("100"), ts_event=2, ts_init=2
    )
    assert isinstance(event, OrderPartiallyFilled)
    assert order.state is OrderState.PARTIALLY_FILLED
    assert order.cum_qty == Decimal("0.5")


def test_record_fill_builds_the_event_from_the_orders_identity_and_the_fill() -> None:
    order = _order()
    order.apply(_submitted())  # records venue_oid "oid-1"
    event = order.record_fill(
        trade_id="v1", quantity=Decimal("2"), price=Decimal("100"), ts_event=7, ts_init=8
    )
    assert event is not None
    assert event.cloid == "0xabc"
    assert event.strategy_id == "trivial"
    assert event.signal_id == "trivial:BTC:1"
    assert event.symbol == "BTC"
    assert event.venue_oid == "oid-1"
    assert event.trade_id == "v1"
    assert event.quantity == Decimal("2")
    assert event.price == Decimal("100")
    assert event.cum_qty == Decimal("2")
    assert event.ts_event == 7
    assert event.ts_init == 8


def test_record_fill_accumulates_parts_and_converges_to_filled() -> None:
    order = _order()
    order.apply(_submitted())
    first = order.record_fill(
        trade_id="v1", quantity=Decimal("1.5"), price=Decimal("100"), ts_event=2, ts_init=2
    )
    second = order.record_fill(
        trade_id="v2", quantity=Decimal("0.5"), price=Decimal("100"), ts_event=3, ts_init=3
    )
    assert isinstance(first, OrderPartiallyFilled)
    assert isinstance(second, OrderFilled)
    assert order.state is OrderState.FILLED
    assert order.cum_qty == Decimal("2")


def test_record_fill_dedups_a_redelivered_trade_id_without_double_counting() -> None:
    order = _order()
    order.apply(_submitted())
    order.record_fill(
        trade_id="v1", quantity=Decimal("1"), price=Decimal("100"), ts_event=2, ts_init=2
    )
    # Same trade_id -> same event_id: a deduped no-op returns None, cum_qty held.
    duplicate = order.record_fill(
        trade_id="v1", quantity=Decimal("1"), price=Decimal("100"), ts_event=3, ts_init=3
    )
    assert duplicate is None
    assert order.state is OrderState.PARTIALLY_FILLED
    assert order.cum_qty == Decimal("1")
