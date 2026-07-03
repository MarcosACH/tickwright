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
