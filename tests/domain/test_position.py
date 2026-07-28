"""The ``Position`` aggregate: average-cost accounting, one fill at a time.

The economic sibling of ``test_order_saga.py`` — a pure ``domain`` value type
driven through its public ``apply``, with zero infrastructure. Every expected
number here is an independently worked example (entry prices and realized PnL
computed by hand from the fill sequence), never a re-derivation of the code.
"""

from decimal import Decimal

import pytest

from tickwright.domain import (
    InvariantViolation,
    OrderFilled,
    OrderFillEvent,
    Position,
    PositionChange,
    Side,
)


def _fill(*, trade_id: str, quantity: str, price: str, symbol: str = "BTC") -> OrderFillEvent:
    """One fill of ``quantity`` @ ``price``. ``Side`` rides the saga, not the event."""
    return OrderFilled(
        ts_event=1_000,
        ts_init=1_000,
        cloid=f"0x{trade_id}",
        strategy_id="alpha",
        signal_id=f"alpha:{symbol}:1",
        symbol=symbol,
        trade_id=trade_id,
        quantity=Decimal(quantity),
        price=Decimal(price),
        cum_qty=Decimal(quantity),
    )


def _position(symbol: str = "BTC") -> Position:
    return Position(strategy_id="alpha", symbol=symbol)


def test_a_buy_fill_opens_a_long_at_the_fill_price() -> None:
    position = _position()

    changes = position.apply(_fill(trade_id="f1", quantity="2", price="100"), side=Side.BUY)

    assert changes == (PositionChange.OPENED,)
    assert position.signed_size == Decimal("2")
    assert position.entry_price == Decimal("100")
    assert position.realized_pnl == Decimal("0")


def test_adding_to_a_position_recomputes_entry_as_a_weighted_average() -> None:
    position = _position()

    position.apply(_fill(trade_id="f1", quantity="2", price="100"), side=Side.BUY)
    changes = position.apply(_fill(trade_id="f2", quantity="6", price="200"), side=Side.BUY)

    # (2 x 100 + 6 x 200) / 8 = 1400 / 8 = 175 — worked by hand, not re-derived.
    assert changes == (PositionChange.CHANGED,)
    assert position.signed_size == Decimal("8")
    assert position.entry_price == Decimal("175")
    assert position.realized_pnl == Decimal("0")


def test_reducing_a_position_realizes_the_closed_leg_and_leaves_the_entry() -> None:
    position = _position()
    position.apply(_fill(trade_id="f1", quantity="8", price="175"), side=Side.BUY)

    changes = position.apply(_fill(trade_id="f2", quantity="3", price="200"), side=Side.SELL)

    # 3 closed at 200 against an entry of 175 -> +75; the remaining 5 stay at 175.
    assert changes == (PositionChange.CHANGED,)
    assert position.signed_size == Decimal("5")
    assert position.entry_price == Decimal("175")
    assert position.realized_pnl == Decimal("75")


def test_a_short_closed_below_its_entry_books_a_profit() -> None:
    """Realized PnL is signed against the *closed exposure*, not the fill side."""
    position = _position()
    position.apply(_fill(trade_id="f1", quantity="4", price="100"), side=Side.SELL)

    changes = position.apply(_fill(trade_id="f2", quantity="4", price="90"), side=Side.BUY)

    # Short 4 @ 100 bought back at 90 -> +40, and the record goes flat.
    assert changes == (PositionChange.CLOSED,)
    assert position.signed_size == Decimal("0")
    assert position.entry_price == Decimal("0")
    assert position.realized_pnl == Decimal("40")
    assert position.is_flat


def test_a_fill_that_flips_through_zero_closes_then_opens_the_residual() -> None:
    position = _position()
    position.apply(_fill(trade_id="f1", quantity="2", price="100"), side=Side.BUY)

    changes = position.apply(_fill(trade_id="f2", quantity="5", price="120"), side=Side.SELL)

    # The whole long leg realizes (2 x (120 - 100) = +40) and the residual -3
    # opens fresh at the fill price — not at a blended entry.
    assert changes == (PositionChange.CLOSED, PositionChange.OPENED)
    assert position.signed_size == Decimal("-3")
    assert position.entry_price == Decimal("120")
    assert position.realized_pnl == Decimal("40")


def test_a_redelivered_fill_is_a_no_op() -> None:
    position = _position()
    fill = _fill(trade_id="f1", quantity="2", price="100")
    position.apply(fill, side=Side.BUY)

    changes = position.apply(fill, side=Side.BUY)

    assert changes == ()
    assert position.signed_size == Decimal("2")
    assert position.entry_price == Decimal("100")


def test_the_ledger_lines_no_slice_moves_yet_read_zero() -> None:
    """``fees``/``funding``/``isolated_collateral`` exist from the first slice so
    the view's shape does not churn when the lines that move them land."""
    position = _position()
    position.apply(_fill(trade_id="f1", quantity="2", price="100"), side=Side.BUY)

    assert position.fees == Decimal("0")
    assert position.funding == Decimal("0")
    assert position.isolated_collateral == Decimal("0")


def test_a_fill_for_another_partition_is_an_invariant_violation() -> None:
    position = _position("BTC")

    with pytest.raises(InvariantViolation):
        position.apply(_fill(trade_id="f1", quantity="1", price="100", symbol="ETH"), side=Side.BUY)
