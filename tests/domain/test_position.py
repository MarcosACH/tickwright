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


def _fill(
    *, trade_id: str, quantity: str, price: str, symbol: str = "BTC", fee: str = "0"
) -> OrderFillEvent:
    """One fill of ``quantity`` @ ``price``. ``Side`` rides the saga, not the event.

    ``fee`` defaults to the frictionless zero, so every case whose subject is
    average-cost accounting states no rate and reads exactly as it did before
    fees existed (ADR-0036).
    """
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
        fee=Decimal(fee),
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
    # The dedup set is contract, not bookkeeping — but a *process-lifetime* one.
    # ADR-0043 §4 rejects persisting it on the position row, so what it promises
    # is that a redelivery *within a run* is a no-op, not that one across a
    # restart is; the atomic checkpoint-plus-ledger write closes that gap.
    assert position.applied_event_ids == frozenset({"0xf1:fill:f1"})


def test_the_ledger_lines_no_slice_moves_yet_read_zero() -> None:
    """``isolated_collateral`` exists from the first slice so the view's shape
    does not churn when the line that moves it lands.

    ``fees`` and ``funding`` were both of these once; each now has a writer of
    its own and is asserted below.
    """
    position = _position()
    position.apply(_fill(trade_id="f1", quantity="2", price="100"), side=Side.BUY)

    assert position.isolated_collateral == Decimal("0")


def test_funding_accrues_on_its_own_line_and_never_into_price_or_pnl() -> None:
    """Funding's own line, and the three places it is not smeared into (ADR-0037).

    The same treatment the fee gets one grain down, and for the same reason:
    ``realized_pnl`` stays **gross**, ``entry_price`` is the price the fill
    traded at and never a carry-adjusted basis, and ``funding`` accumulates on
    its own. The two accruals differ only in that this one arrives on no fill at
    all, which is why it has a verb rather than a field on one.

    A payment and a credit, because the line is signed and the aggregate never
    asks which: the sign arrives already decided by the venue that reported it
    or by the generator that reproduced its formula.
    """
    position = _position()
    position.apply(_fill(trade_id="f1", quantity="2", price="100"), side=Side.BUY)

    position.accrue_funding(Decimal("-0.5"))
    position.accrue_funding(Decimal("0.2"))

    assert position.funding == Decimal("-0.3")
    assert position.entry_price == Decimal("100")
    assert position.realized_pnl == Decimal("0")
    assert position.fees == Decimal("0")
    assert position.view().funding == Decimal("-0.3")


def test_funding_accrued_while_open_survives_the_close_that_flattens_the_position() -> None:
    """It is realized cash, never reversed (ADR-0037).

    A flat record is still a record (P1 #119), and funding is the line that most
    invites the opposite reading: it was charged *for holding* something no
    longer held. But the payment left the account when the boundary settled, so
    unwinding it on the close would be inventing a refund the venue never made.
    """
    position = _position()
    position.apply(_fill(trade_id="f1", quantity="2", price="100"), side=Side.BUY)
    position.accrue_funding(Decimal("-0.5"))

    position.apply(_fill(trade_id="f2", quantity="2", price="150"), side=Side.SELL)

    assert position.is_flat
    assert position.entry_price == Decimal("0")  # reset by the close
    assert position.funding == Decimal("-0.5")  # retained through it


def test_a_fills_fee_accrues_on_its_own_line_and_never_into_price_or_pnl() -> None:
    """The three places a fee could have been smeared into, and is not (ADR-0036).

    ``fees`` is its own cumulative line; ``realized_pnl`` stays **gross** of it,
    as the venue keeps ``closedPnl`` separate; and ``entry_price`` is the price
    the fill traded at, never a cost basis loaded with charges (ADR-0045 §3).
    The sequence closes flat on purpose — a flat record still retains its
    ledger lines (P1 #119), so the fee must survive the close that resets the
    entry.
    """
    position = _position()

    position.apply(_fill(trade_id="f1", quantity="2", price="100", fee="0.09"), side=Side.BUY)
    assert position.entry_price == Decimal("100")  # the traded price, not 100.045

    changes = position.apply(
        _fill(trade_id="f2", quantity="2", price="150", fee="0.135"), side=Side.SELL
    )

    assert changes == (PositionChange.CLOSED,)
    assert position.fees == Decimal("0.225")  # 0.09 + 0.135, per trade, accumulated
    assert position.realized_pnl == Decimal("100")  # 2 x (150 - 100), gross of the 0.225
    assert position.entry_price == Decimal("0")  # reset by the close, carrying no fee
    assert position.is_flat


def test_a_maker_rebate_accrues_as_a_negative_fee() -> None:
    """A signed line, so a credit is the same arithmetic as a debit (ADR-0036).

    The aggregate never asks which side of the book the fill came from — the
    sign arrives already decided by the venue that reported it, which is why
    nothing here says "maker".
    """
    position = _position()

    position.apply(_fill(trade_id="f1", quantity="2", price="100", fee="-0.03"), side=Side.BUY)

    assert position.fees == Decimal("-0.03")


def test_a_redelivered_fill_charges_its_fee_once() -> None:
    """The fee rides the dedup that guards the size, because it sits behind it.

    ``apply`` is the fill's one gatekeeper, so at-least-once redelivery
    (ADR-0025) cannot charge twice. Asserted on an *adding* fill, where a second
    application would otherwise leave both the size and the fee line doubled —
    and doubled by the same amount, which a totals-only check would miss.
    """
    position = _position()
    fill = _fill(trade_id="f1", quantity="2", price="100", fee="0.09")
    position.apply(fill, side=Side.BUY)

    assert position.apply(fill, side=Side.BUY) == ()
    assert position.signed_size == Decimal("2")
    assert position.fees == Decimal("0.09")


def test_a_fill_for_another_symbol_is_an_invariant_violation() -> None:
    """A misrouted fill is a broken engine assumption, not something to absorb:
    silently applying it would corrupt two partitions at once (ADR-0014)."""
    position = _position("BTC")

    with pytest.raises(InvariantViolation, match="applied to position"):
        position.apply(_fill(trade_id="f1", quantity="1", price="100", symbol="ETH"), side=Side.BUY)


def test_a_fill_for_another_strategy_is_an_invariant_violation() -> None:
    """The partition is ``(strategy, symbol)``, so the strategy half is checked
    too — otherwise one strategy's flow could land in another's attribution."""
    position = Position(strategy_id="beta", symbol="BTC")

    with pytest.raises(InvariantViolation, match="applied to position"):
        position.apply(_fill(trade_id="f1", quantity="1", price="100"), side=Side.BUY)


def test_a_zero_quantity_fill_is_an_invariant_violation() -> None:
    """Booking one would open a *flat* record at a non-zero entry price — the
    aggregate's own documented invariant says an entry is meaningful only while
    non-flat. There is no state for "open at zero size", so refuse the fill
    rather than manufacture one (ADR-0014).

    ``match`` is what keeps the assertion pinned to *this* guard: ``apply`` has
    two refusal sites and one exception type, so a type-only assertion would
    read green on a misroute that fired for an unrelated reason."""
    position = _position()

    with pytest.raises(InvariantViolation, match="non-positive quantity"):
        position.apply(_fill(trade_id="f1", quantity="0", price="100"), side=Side.BUY)


def test_a_negative_quantity_fill_is_an_invariant_violation() -> None:
    """A fill's quantity is a magnitude — ``side`` carries the direction, so a
    negative one would invert it behind the saga's back and book a sell as a
    buy. The guard is on the magnitude, not just on zero."""
    position = _position()

    with pytest.raises(InvariantViolation, match="non-positive quantity"):
        position.apply(_fill(trade_id="f1", quantity="-1", price="100"), side=Side.BUY)


def test_a_refused_fill_leaves_the_ledger_exactly_as_it_was() -> None:
    """The guard precedes the dedup, so a refusal is atomic in both directions:
    it moves no line, and it does not burn the ``event_id``. A producer that
    corrects a bad quantity and re-sends the same trade must still be booked,
    not silently swallowed as a redelivery (ADR-0025).

    The refused fill carries a fee, so "moves no line" covers the fee line too:
    the accrual sits *behind* both guards, and a fill the aggregate never booked
    must not leave a charge for a trade that did not happen (ADR-0036).
    """
    position = _position()
    position.apply(_fill(trade_id="f1", quantity="2", price="100"), side=Side.BUY)

    with pytest.raises(InvariantViolation, match="non-positive quantity"):
        position.apply(_fill(trade_id="f2", quantity="0", price="500", fee="7.5"), side=Side.BUY)

    assert position.signed_size == Decimal("2")
    assert position.entry_price == Decimal("100")
    assert position.fees == Decimal("0")
    assert "0xf2:fill:f2" not in position.applied_event_ids

    assert position.apply(_fill(trade_id="f2", quantity="2", price="200"), side=Side.BUY) == (
        PositionChange.CHANGED,
    )
    assert position.entry_price == Decimal("150")
