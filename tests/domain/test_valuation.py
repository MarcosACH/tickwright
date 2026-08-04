"""Tier-2 view assembly: the formulas and the per-term nullability rule.

A pure ``domain`` module, so these run with no engine, no store and no venue —
which is the whole reason the arithmetic lives behind a function signature
rather than inside the projection (ADR-0035). Every expected number is worked by
hand from the position and the mark, never re-derived the way the code derives
it.

The rule under test throughout is ADR-0041 §6's: nullability is **per-term, not
per-field**. A Tier-2 field reads ``None`` only when the mark is absent *and its
own terms need it*, so a flat position still reads a real ``0`` — because
``0 × (mark − entry)`` and ``|0| × mark`` are zero at every mark, including one
nobody has seen.
"""

from decimal import Decimal

from tickwright.domain import (
    Account,
    OrderFilled,
    Position,
    Side,
    account_view,
    position_view,
)


def _position(*, quantity: str, price: str, side: Side, symbol: str = "BTC") -> Position:
    position = Position(strategy_id="alpha", symbol=symbol)
    position.apply(
        OrderFilled(
            ts_event=1_000,
            ts_init=1_000,
            cloid="0xf1",
            strategy_id="alpha",
            signal_id=f"alpha:{symbol}:1",
            symbol=symbol,
            trade_id="f1",
            quantity=Decimal(quantity),
            price=Decimal(price),
            cum_qty=Decimal(quantity),
            fee=Decimal("0"),
        ),
        side=side,
    )
    return position


def _account(cash: str = "100000") -> Account:
    return Account(account_id="paper-default", genesis_collateral=Decimal(cash), genesis_ts_ns=7)


def test_a_held_position_with_no_mark_reads_unknown_rather_than_worthless() -> None:
    """The recovery-window case: a restart has its Tier-1 ledger back before any
    mark has arrived. Reporting ``0`` there would say the position is worth
    nothing, which is a number a strategy can act on and is false."""
    position = _position(quantity="2", price="100", side=Side.BUY)

    view = position_view(position, account_net=Decimal("2"), mark=None, mark_ts=None)

    assert view.unrealized_pnl is None
    assert view.notional is None
    assert view.mark_ts is None
    # Tier-1 is unaffected — it never depended on the mark (ADR-0041 §6).
    assert view.size == Decimal("2")
    assert view.entry_price == Decimal("100")
    assert view.realized_pnl == Decimal("0")


def test_a_flat_position_with_no_mark_still_reads_real_zeros() -> None:
    """Traded to flat and never marked since. Both terms are zero at every mark,
    so withholding them would answer ``None`` to a question nothing was missing
    for — and a flat-with-history record is exactly what a restart holds most of.
    """
    position = _position(quantity="2", price="100", side=Side.BUY)
    position.apply(
        OrderFilled(
            ts_event=2_000,
            ts_init=2_000,
            cloid="0xf2",
            strategy_id="alpha",
            signal_id="alpha:BTC:2",
            symbol="BTC",
            trade_id="f2",
            quantity=Decimal("2"),
            price=Decimal("150"),
            cum_qty=Decimal("2"),
            fee=Decimal("0"),
        ),
        side=Side.SELL,
    )

    view = position_view(position, account_net=Decimal("0"), mark=None, mark_ts=None)

    assert view.unrealized_pnl == Decimal("0")
    assert view.notional == Decimal("0")
    assert view.realized_pnl == Decimal("100")  # 2 x (150 - 100), retained


def test_the_two_terms_take_their_zero_from_different_facts() -> None:
    """``unrealized_pnl`` is exempt on a flat **own slice**; ``notional`` on a
    flat **account net**. The two come apart under offsetting partitions: the
    account holds nothing in the symbol, so its exposure really is zero, while
    this partition's own leg is open and genuinely needs a mark to value.
    """
    position = _position(quantity="2", price="100", side=Side.BUY)

    view = position_view(position, account_net=Decimal("0"), mark=None, mark_ts=None)

    assert view.notional == Decimal("0")
    assert view.unrealized_pnl is None


def test_account_equity_needs_no_mark_when_every_partition_is_flat() -> None:
    """The Σ's per-term rule, matching the per-position one: a flat partition
    contributes its real zero and never blocks the total."""
    flat = _position(quantity="2", price="100", side=Side.BUY)
    flat.apply(
        OrderFilled(
            ts_event=2_000,
            ts_init=2_000,
            cloid="0xf2",
            strategy_id="alpha",
            signal_id="alpha:BTC:2",
            symbol="BTC",
            trade_id="f2",
            quantity=Decimal("2"),
            price=Decimal("150"),
            cum_qty=Decimal("2"),
            fee=Decimal("0"),
        ),
        side=Side.SELL,
    )

    view = account_view(_account("1000"), positions=(flat,), marks={})

    assert view.equity == Decimal("1000")
