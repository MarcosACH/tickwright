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
    LeverageSpec,
    OrderFilled,
    Position,
    Side,
    account_view,
    position_view,
)

CROSS_10X = LeverageSpec(mode="cross", leverage=10)
ISOLATED_1X = LeverageSpec(mode="isolated", leverage=1)
ISOLATED_5X = LeverageSpec(mode="isolated", leverage=5)


def _position(
    *,
    quantity: str,
    price: str,
    side: Side,
    symbol: str = "BTC",
    isolated_collateral: str = "0",
) -> Position:
    position = Position(
        strategy_id="alpha", symbol=symbol, isolated_collateral=Decimal(isolated_collateral)
    )
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

    view = position_view(
        position, account_net=Decimal("2"), mark=None, mark_ts=None, leverage=CROSS_10X
    )

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

    view = position_view(
        position, account_net=Decimal("0"), mark=None, mark_ts=None, leverage=CROSS_10X
    )

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

    view = position_view(
        position, account_net=Decimal("0"), mark=None, mark_ts=None, leverage=CROSS_10X
    )

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


def test_a_cross_position_posts_its_notional_divided_by_the_configured_leverage() -> None:
    """Cross ``margin_used`` is ``notional / leverage`` (ADR-0040 §2), off the
    **account-net** exposure rather than this partition's own slice: the venue
    holds one position per symbol and one collateral bucket behind it.

    Worked by hand: ``|0.5| x 60000 = 30000`` of notional at ``10x`` posts
    ``3000``. There is no venue-side haircut on the initial fraction, so the
    amount is the division directly (ADR-0040 §4, "No ``margin_init`` field").
    """
    position = _position(quantity="0.5", price="58000", side=Side.BUY)

    view = position_view(
        position,
        account_net=Decimal("0.5"),
        mark=Decimal("60000"),
        mark_ts=9_000,
        leverage=CROSS_10X,
    )

    assert view.notional == Decimal("30000")
    assert view.margin_used == Decimal("3000")


def test_an_isolated_position_posts_its_locked_collateral_plus_unrealized_pnl() -> None:
    """Isolated ``margin_used`` is ``isolated_collateral + unrealized_pnl`` and
    **moves with the mark** (ADR-0040 §3, corrected by #142) — a different rule
    from cross's ``notional / leverage``, not a re-parameterisation of it: the
    configured leverage is not a term, because a bucket's collateral is fixed at
    open and a later leverage change never re-margins it.

    The two expectations are the venue's own ``marginUsed`` for the #142 testnet
    position, read off ``clearinghouseState`` at two marks two dollars apart:
    ``25.860067`` at 64796 and ``25.856067`` at 64794, against a locked
    ``25.898067`` behind ``+0.002`` entered at 64815. They are transcribed
    literals, not this formula run twice.
    """
    position = _position(
        quantity="0.002", price="64815", side=Side.BUY, isolated_collateral="25.898067"
    )

    at_64796 = position_view(
        position,
        account_net=Decimal("0.002"),
        mark=Decimal("64796"),
        mark_ts=9_000,
        leverage=ISOLATED_5X,
    )
    at_64794 = position_view(
        position,
        account_net=Decimal("0.002"),
        mark=Decimal("64794"),
        mark_ts=9_003,
        leverage=ISOLATED_5X,
    )

    assert at_64796.margin_used == Decimal("25.860067")
    assert at_64794.margin_used == Decimal("25.856067")
