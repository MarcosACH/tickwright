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
    InstrumentSpec,
    LeverageBook,
    LeverageSpec,
    LiquidationSource,
    OrderFilled,
    Position,
    PositionView,
    Side,
    account_view,
    position_view,
)

CROSS_1X = LeverageSpec(mode="cross", leverage=1)
CROSS_10X = LeverageSpec(mode="cross", leverage=10)
ISOLATED_1X = LeverageSpec(mode="isolated", leverage=1)
ISOLATED_5X = LeverageSpec(mode="isolated", leverage=5)

BTC_40X = InstrumentSpec(
    symbol="BTC",
    sz_decimals=5,
    max_decimals=6,
    min_notional=Decimal("10"),
    max_leverage=40,
    margin_maint=Decimal("0.0125"),  # 1/(2 x 40), the venue's tier-0 rate
)


ETH_25X = InstrumentSpec(
    symbol="ETH",
    sz_decimals=4,
    max_decimals=6,
    min_notional=Decimal("10"),
    max_leverage=25,
    margin_maint=Decimal("0.02"),  # 1/(2 x 25)
)


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
        position,
        account_net=Decimal("2"),
        account_unrealized_pnl=None,
        account_equity=None,
        mark=None,
        mark_ts=None,
        leverage=CROSS_10X,
        spec=None,
        liquidation=LiquidationSource.computed(),
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
        position,
        account_net=Decimal("0"),
        account_unrealized_pnl=None,
        account_equity=None,
        mark=None,
        mark_ts=None,
        leverage=CROSS_10X,
        spec=None,
        liquidation=LiquidationSource.computed(),
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
        position,
        account_net=Decimal("0"),
        account_unrealized_pnl=None,
        account_equity=None,
        mark=None,
        mark_ts=None,
        leverage=CROSS_10X,
        spec=None,
        liquidation=LiquidationSource.computed(),
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

    view = account_view(
        _account("1000"), positions=(flat,), marks={}, leverage=LeverageBook(), specs={}
    )

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
        account_unrealized_pnl=Decimal("1000"),  # 0.5 x (60000 - 58000), one partition
        account_equity=Decimal("101000"),
        mark=Decimal("60000"),
        mark_ts=9_000,
        leverage=CROSS_10X,
        spec=BTC_40X,
        liquidation=LiquidationSource.computed(),
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
        account_unrealized_pnl=Decimal("-0.038"),  # 0.002 x (64796 - 64815)
        account_equity=Decimal("99999.962"),
        mark=Decimal("64796"),
        mark_ts=9_000,
        leverage=ISOLATED_5X,
        spec=BTC_40X,
        liquidation=LiquidationSource.computed(),
    )
    at_64794 = position_view(
        position,
        account_net=Decimal("0.002"),
        account_unrealized_pnl=Decimal("-0.042"),  # 0.002 x (64794 - 64815)
        account_equity=Decimal("99999.958"),
        mark=Decimal("64794"),
        mark_ts=9_003,
        leverage=ISOLATED_5X,
        spec=BTC_40X,
        liquidation=LiquidationSource.computed(),
    )

    assert at_64796.margin_used == Decimal("25.860067")
    assert at_64794.margin_used == Decimal("25.856067")


def test_maintenance_margin_is_the_flat_tier_zero_rate_on_notional() -> None:
    """``notional × margin_maint``, the rate carried as spec data rather than
    derived, so ``domain`` never learns the venue's "half the initial margin at
    max leverage" rule (ADR-0040 §4).

    The pair is #152's re-verification below testnet BTC's first margin-tier
    band, where the flat rate *is* the venue's own: notional ``5873.49``
    reported maintenance ``73.418625`` at exactly ``1/80``. Both transcribed.
    """
    position = _position(quantity="0.09", price="64000", side=Side.BUY)

    view = position_view(
        position,
        account_net=Decimal("0.09"),
        account_unrealized_pnl=Decimal("113.49"),  # 0.09 x (65261 - 64000)
        account_equity=Decimal("100113.49"),
        mark=Decimal("65261"),
        mark_ts=9_000,
        leverage=CROSS_10X,
        spec=BTC_40X,
        liquidation=LiquidationSource.computed(),
    )

    assert view.notional == Decimal("5873.49")
    assert view.maintenance_margin == Decimal("73.418625")


def test_maintenance_margin_stays_flat_above_the_first_tier_band() -> None:
    """Tier-0 is applied **flat**, and above the first band that is knowingly
    not the venue's number — ADR-0040 §4 defers the tier table and lets the
    divergence trip §6's alert rather than absorbing it.

    #152's measured crossing: a 0.185 BTC testnet long at notional
    ``12073.655`` sits in the ``$10k``–``$50k`` band, where the venue reports
    ``166.4731`` from ``notional × 0.02 − 75``. We report the flat
    ``150.9206875`` and under-report by ``15.55``, on purpose. A change here
    that starts reproducing ``166.4731`` is a tier implementation, and it must
    arrive with the alert band re-reasoned rather than as a silent fix.
    """
    position = _position(quantity="0.185", price="64000", side=Side.BUY)

    view = position_view(
        position,
        account_net=Decimal("0.185"),
        account_unrealized_pnl=Decimal("233.655"),  # 0.185 x (65263 - 64000)
        account_equity=Decimal("100233.655"),
        mark=Decimal("65263"),
        mark_ts=9_000,
        leverage=CROSS_10X,
        spec=BTC_40X,
        liquidation=LiquidationSource.computed(),
    )

    assert view.notional == Decimal("12073.655")
    assert view.maintenance_margin == Decimal("150.9206875")


def test_a_flat_account_net_reads_a_real_zero_maintenance_with_no_mark_and_no_spec() -> None:
    """The per-term rule reaches the *rate* term too, not just the mark.

    ``notional × margin_maint`` is zero whenever the notional is, whatever the
    rate — so a symbol the account nets flat in needs no ``InstrumentSpec`` to
    answer, and the reserved unattributed partition holding a symbol outside our
    universe is exactly where both terms go missing at once (ADR-0041 §6).
    Answering ``None`` here would withhold a number that is not in doubt.
    """
    position = _position(quantity="2", price="100", side=Side.BUY)

    view = position_view(
        position,
        account_net=Decimal("0"),
        account_unrealized_pnl=None,
        account_equity=None,
        mark=None,
        mark_ts=None,
        leverage=CROSS_10X,
        spec=None,
        liquidation=LiquidationSource.computed(),
    )

    assert view.notional == Decimal("0")
    assert view.maintenance_margin == Decimal("0")


def test_the_per_term_rule_holds_in_both_margin_modes() -> None:
    """Cross and isolated compute ``margin_used`` from different terms, so the
    nullability rule has to be re-checked on each rather than inherited from
    whichever mode was written first (ADR-0041 §6).

    Both directions, on both modes: a genuinely closed position reads real
    zeros with no mark ever seen, and a held one reads ``None`` — cross because
    ``notional`` needs the mark, isolated because ``unrealized_pnl`` does.
    """
    closed = _position(quantity="2", price="100", side=Side.BUY)
    closed.apply(
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
    held = _position(quantity="2", price="100", side=Side.BUY)

    for mode in (CROSS_10X, ISOLATED_1X):
        flat_view = position_view(
            closed,
            account_net=Decimal("0"),
            account_unrealized_pnl=Decimal("0"),
            account_equity=Decimal("100000"),
            mark=None,
            mark_ts=None,
            leverage=mode,
            spec=BTC_40X,
            liquidation=LiquidationSource.computed(),
        )
        held_view = position_view(
            held,
            account_net=Decimal("2"),
            account_unrealized_pnl=None,
            account_equity=None,
            mark=None,
            mark_ts=None,
            leverage=mode,
            spec=BTC_40X,
            liquidation=LiquidationSource.computed(),
        )

        assert flat_view.margin_used == Decimal("0"), mode
        assert flat_view.maintenance_margin == Decimal("0"), mode
        assert held_view.margin_used is None, mode
        assert held_view.maintenance_margin is None, mode


def test_the_view_reports_position_grain_economics_beside_the_own_slice() -> None:
    """ADR-0041 §4's two grains in one view, and the fields that carry each.

    ``unrealized_pnl`` is the **own slice** — the fills this strategy placed.
    ``margin_used``, ``notional``, ``leverage`` and ``margin_mode`` describe the
    **whole venue position**: one collateral bucket, one ``liquidationPx``, keyed
    per position and never per strategy. Isolated ``margin_used`` therefore takes
    its uPnL term at account-net grain, not from the field beside it.

    Foreign flow is where the two come apart in v1 (§5). This strategy holds
    ``+0.002`` entered at 64815; the reserved unattributed partition holds a
    further ``+0.001`` entered at 64000, so the account nets ``+0.003``. Worked
    by hand at mark 64796, the two legs separately:

        own slice   0.002 x (64796 - 64815) = -0.038
        foreign     0.001 x (64796 - 64000) = +0.796
        account net                            +0.758

    so the bucket reports ``25.898067 + 0.758 = 26.656067`` while the strategy's
    own line still reads ``-0.038``.
    """
    position = _position(
        quantity="0.002", price="64815", side=Side.BUY, isolated_collateral="25.898067"
    )

    view = position_view(
        position,
        account_net=Decimal("0.003"),
        account_unrealized_pnl=Decimal("0.758"),
        account_equity=Decimal("100000.758"),
        mark=Decimal("64796"),
        mark_ts=9_000,
        leverage=ISOLATED_5X,
        spec=BTC_40X,
        liquidation=LiquidationSource.computed(),
    )

    assert view.unrealized_pnl == Decimal("-0.038")
    assert view.margin_used == Decimal("26.656067")
    assert view.leverage == 5
    assert view.margin_mode == "isolated"


def test_effective_leverage_divides_notional_by_each_modes_own_backing() -> None:
    """The realized exposure ratio, whose denominator splits by mode (ADR-0041
    §4.1): the position's own bucket marked to market for isolated, whole-account
    equity for cross.

    The isolated arm is #142's ``updateIsolatedMargin`` top-up, the measurement
    that adjudicated the modelling choice — a +20 USDC deposit behind an
    unchanged ``+0.002`` at mark 64794 drove the venue's ratio from ``5.0119``
    to ``2.8260``. Both are transcribed from the ADR-0041 §4.1 amendment, and the
    account equity is held **fixed** across the pair on purpose: under an
    account-equity denominator the top-up would leave the ratio untouched, so the
    move is what discriminates the two candidate denominators rather than merely
    agreeing with one.

    The cross arm is worked by hand instead: ``30000`` of notional against
    ``12000`` of account equity is ``2.5``, an account-grain denominator that
    the position's own ``isolated_collateral`` plays no part in.
    """
    held = _position(
        quantity="0.002", price="64815", side=Side.BUY, isolated_collateral="25.898067"
    )
    topped_up = _position(
        quantity="0.002", price="64815", side=Side.BUY, isolated_collateral="45.898067"
    )

    def at_64794(position: Position) -> PositionView:
        return position_view(
            position,
            account_net=Decimal("0.002"),
            account_unrealized_pnl=Decimal("-0.042"),  # 0.002 x (64794 - 64815)
            account_equity=Decimal("1000"),
            mark=Decimal("64794"),
            mark_ts=9_000,
            leverage=ISOLATED_5X,
            spec=BTC_40X,
            liquidation=LiquidationSource.computed(),
        )

    before = at_64794(held)
    after = at_64794(topped_up)

    assert before.notional == Decimal("129.588")
    assert before.effective_leverage is not None
    assert before.effective_leverage.quantize(Decimal("0.0001")) == Decimal("5.0119")
    assert after.effective_leverage is not None
    assert after.effective_leverage.quantize(Decimal("0.0001")) == Decimal("2.8260")
    assert after.effective_leverage < before.effective_leverage

    cross = position_view(
        _position(quantity="0.5", price="58000", side=Side.BUY),
        account_net=Decimal("0.5"),
        account_unrealized_pnl=Decimal("1000"),
        account_equity=Decimal("12000"),
        mark=Decimal("60000"),
        mark_ts=9_000,
        leverage=CROSS_10X,
        spec=BTC_40X,
        liquidation=LiquidationSource.computed(),
    )

    assert cross.notional == Decimal("30000")
    assert cross.effective_leverage == Decimal("2.5")


def test_effective_leverage_reads_none_on_a_non_positive_denominator() -> None:
    """The one Tier-2 ``None`` a fresh mark cannot cure (ADR-0041 §6).

    Every other nullable field here is missing an *input*; this one is undefined
    even with every input present, because a backing collateral of zero or less has
    no ratio to it. And the state is reached by ordinary trading rather than by
    accident: paper reports a negative free margin without rejecting an order or
    liquidating anything (ADR-0040 §7), so an account really does keep running
    past the point where its equity — or an isolated bucket's — goes through
    zero. A negative ratio would read as *de-levered*, the opposite of the truth.

    The rest of the view stays real there, which is what makes this a property of
    the ratio rather than of the position: a wiped account still has a notional,
    an unrealized PnL and margins, and every one of them is a number.

    Zero is the same answer as negative in the **denominator** and the opposite
    one in the numerator (ADR-0041 §3): a flat cross position on a solvent
    account reads a real ``0`` — no exposure over real equity — while a flat
    isolated one reads ``None``, its collateral having been released with the
    position.
    """
    wiped_cross = position_view(
        _position(quantity="0.5", price="58000", side=Side.BUY),
        account_net=Decimal("0.5"),
        account_unrealized_pnl=Decimal("1000"),
        account_equity=Decimal("-500"),  # past zero, still trading (ADR-0040 §7)
        mark=Decimal("60000"),
        mark_ts=9_000,
        leverage=CROSS_10X,
        spec=BTC_40X,
        liquidation=LiquidationSource.computed(),
    )

    assert wiped_cross.effective_leverage is None
    assert wiped_cross.notional == Decimal("30000")
    assert wiped_cross.unrealized_pnl == Decimal("1000")
    assert wiped_cross.margin_used == Decimal("3000")
    assert wiped_cross.maintenance_margin == Decimal("375")  # 30000 x 1/80

    wiped_bucket = position_view(
        _position(quantity="0.002", price="64815", side=Side.BUY, isolated_collateral="25.898067"),
        account_net=Decimal("0.002"),
        account_unrealized_pnl=Decimal("-30"),  # more than the bucket holds
        account_equity=Decimal("100000"),
        mark=Decimal("64794"),
        mark_ts=9_000,
        leverage=ISOLATED_5X,
        spec=BTC_40X,
        liquidation=LiquidationSource.computed(),
    )

    assert wiped_bucket.effective_leverage is None
    assert wiped_bucket.margin_used == Decimal("-4.101933")  # 25.898067 - 30, reported as-is
    assert wiped_bucket.notional == Decimal("129.588")

    flat_cross = position_view(
        _position(quantity="2", price="100", side=Side.BUY),
        account_net=Decimal("0"),
        account_unrealized_pnl=Decimal("0"),
        account_equity=Decimal("100000"),
        mark=Decimal("150"),
        mark_ts=9_000,
        leverage=CROSS_10X,
        spec=BTC_40X,
        liquidation=LiquidationSource.computed(),
    )
    flat_isolated = position_view(
        _position(quantity="2", price="100", side=Side.BUY),
        account_net=Decimal("0"),
        account_unrealized_pnl=Decimal("0"),
        account_equity=Decimal("100000"),
        mark=Decimal("150"),
        mark_ts=9_000,
        leverage=ISOLATED_1X,  # collateral released with the position
        spec=BTC_40X,
        liquidation=LiquidationSource.computed(),
    )

    assert flat_cross.effective_leverage == Decimal("0")
    assert flat_isolated.effective_leverage is None


def test_the_paper_isolated_liquidation_price_is_computed_off_the_marked_bucket() -> None:
    """``liq = mark − side · margin_available / size / (1 − l · side)`` (ADR-0040
    §3), where isolated's ``margin_available`` is
    ``(isolated_collateral + unrealized_pnl) − maintenance_margin``.

    The unrealized-PnL term is the half #142 had to correct: reading the bucket
    as static collateral alone put the price 21.27 units low. Both expectations
    are the venue's own ``liquidationPx`` for that testnet position, transcribed
    — ``52522.4977`` behind ``25.898067`` of collateral, and ``42395.9154`` after
    an ``updateIsolatedMargin`` top-up of +20 moved it out of the way.

    The third read is the same position at a mark two dollars away, and it is not
    a fourth measurement but an **invariant**: the formula's mark terms cancel,
    so a moving mark must leave the price exactly where it was. That is an
    independent check on the arithmetic — a formula that reproduced the two
    literals but drifted with the mark would be wrong in a way the literals alone
    cannot see (#142 confirmed the invariance against the venue).
    """
    held = _position(
        quantity="0.002", price="64815", side=Side.BUY, isolated_collateral="25.898067"
    )
    topped_up = _position(
        quantity="0.002", price="64815", side=Side.BUY, isolated_collateral="45.898067"
    )

    def at(position: Position, mark: str, account_unrealized_pnl: str) -> PositionView:
        return position_view(
            position,
            account_net=Decimal("0.002"),
            account_unrealized_pnl=Decimal(account_unrealized_pnl),
            account_equity=Decimal("100000"),
            mark=Decimal(mark),
            mark_ts=9_000,
            leverage=ISOLATED_5X,
            spec=BTC_40X,
            liquidation=LiquidationSource.computed(),
        )

    before = at(held, "64794", "-0.042")  # 0.002 x (64794 - 64815)
    after = at(topped_up, "64794", "-0.042")
    two_dollars_away = at(held, "64796", "-0.038")  # 0.002 x (64796 - 64815)

    assert before.liquidation_price is not None
    assert before.liquidation_price.quantize(Decimal("0.0001")) == Decimal("52522.4977")
    assert after.liquidation_price is not None
    assert after.liquidation_price.quantize(Decimal("0.0001")) == Decimal("42395.9154")
    assert two_dollars_away.liquidation_price == before.liquidation_price


def test_the_paper_cross_liquidation_price_is_computed_off_account_equity() -> None:
    """Cross's ``margin_available`` is ``equity − maintenance_margin`` (ADR-0040
    §3) — the shared pool, not a per-position bucket.

    Both expectations are derived from the **defining condition** rather than
    from the formula under test: liquidation is where the account's equity, moved
    to the liquidation price, has fallen to the maintenance margin owed there.
    For ``+0.5`` at a mark of 60000 on 12000 of equity, at rate ``1/80``:

        12000 + 0.5·(P − 60000) = 0.5·P·0.0125   ->   0.49375·P = 18000
                                                 ->   P = 36455.6962

    and for the mirrored short, whose price sits **above** the mark, since a
    short is liquidated by a rally (ADR-0046 §6):

        12000 − 0.5·(P − 60000) = 0.5·P·0.0125   ->   0.50625·P = 42000
                                                 ->   P = 82962.9630

    A flat account-net has no price at all: the formula divides by size, and a
    flat position has no side to divide with (ADR-0041 §3).
    """

    def at(side: Side, account_net: str, account_unrealized_pnl: str) -> PositionView:
        return position_view(
            _position(quantity="0.5", price="58000", side=side),
            account_net=Decimal(account_net),
            account_unrealized_pnl=Decimal(account_unrealized_pnl),
            account_equity=Decimal("12000"),
            mark=Decimal("60000"),
            mark_ts=9_000,
            leverage=CROSS_10X,
            spec=BTC_40X,
            liquidation=LiquidationSource.computed(),
        )

    long = at(Side.BUY, "0.5", "1000")  # 0.5 x (60000 - 58000)
    short = at(Side.SELL, "-0.5", "-1000")
    flat = at(Side.BUY, "0", "0")

    assert long.liquidation_price is not None
    assert long.liquidation_price.quantize(Decimal("0.0001")) == Decimal("36455.6962")
    assert short.liquidation_price is not None
    assert short.liquidation_price.quantize(Decimal("0.0001")) == Decimal("82962.9630")
    assert short.liquidation_price > Decimal("60000")
    assert flat.liquidation_price is None


def test_a_computed_liquidation_price_at_or_below_zero_reads_none() -> None:
    """The venue reports ``null`` when the price would be non-positive, and paper
    must not answer a negative number where live answers nothing (ADR-0046 §6).

    This is the **majority** case for a long rather than a corner — 12 of 17
    cross longs across 22 mainnet accounts read ``null`` — so the two paths
    would disagree on most of the book, which is exactly the compute asymmetry
    ADR-0034's shared grain forbids. It is structurally unreachable for a short,
    whose price sits above the mark.

    The threshold is **zero**, not the venue's minimum tick: zero is the real
    boundary, where a long's collateral is exhausted, and clamping at a tick
    would invent a liquidation price for a position that has none. Both
    expectations below come from the defining condition — equity moved to ``P``
    equals the maintenance owed there — never from the formula under test:

        equity = 30000:    30000 + 0.5·(P − 60000) = 0.5·P·0.0125
                           ->  0.49375·P = 0        ->  P = 0        -> None
        equity = 29999.9:  ->  0.49375·P = 0.1      ->  P = 0.2025   -> reported

    A tenth of a dollar of equity separates them, and the second price is a fifth
    of BTC's own $1 mark quantum — still a number, because the position really
    does have a level at which it is closed out.
    """

    def at(equity: str, *, side: Side = Side.BUY, account_net: str = "0.5") -> PositionView:
        return position_view(
            _position(quantity="0.5", price="58000", side=side),
            account_net=Decimal(account_net),
            account_unrealized_pnl=Decimal("1000"),
            account_equity=Decimal(equity),
            mark=Decimal("60000"),
            mark_ts=9_000,
            leverage=CROSS_10X,
            spec=BTC_40X,
            liquidation=LiquidationSource.computed(),
        )

    well_collateralized = at("101000")

    assert well_collateralized.liquidation_price is None
    assert well_collateralized.notional == Decimal("30000")
    assert well_collateralized.margin_used == Decimal("3000")
    assert well_collateralized.maintenance_margin == Decimal("375")

    assert at("30000").liquidation_price is None

    just_above = at("29999.9").liquidation_price
    assert just_above is not None
    assert just_above.quantize(Decimal("0.0001")) == Decimal("0.2025")

    short = at("101000", side=Side.SELL, account_net="-0.5")
    assert short.liquidation_price is not None
    assert short.liquidation_price > Decimal("60000")


def test_the_account_totals_sum_the_position_grain_numbers() -> None:
    """``AccountView``'s four Tier-2 aggregates (ADR-0041 §4): the two Σs, the
    free margin they leave, and the account's own realized leverage.

    Worked by hand over two symbols in the two modes, so neither rule is the
    only one exercised. On 100000 of cash:

        BTC  cross 10x   +0.5 @ 58000, mark 60000
                         uPnL 1000, notional 30000
                         margin_used 30000/10 = 3000, maint 30000/80 = 375
        ETH  isolated 5x +10 @ 3000, mark 3200, bucket 6000
                         uPnL 2000, notional 32000
                         margin_used 6000 + 2000 = 8000, maint 32000/50 = 640

        equity      100000 + 1000 + 2000 = 103000
        Σ margin    3000 + 8000          =  11000
        Σ maint     375 + 640            =   1015
        free        103000 − 11000       =  92000
        leverage    62000 / 103000       = 0.6019

    ``free_margin`` subtracts the Σ of *margin used*, not of maintenance: it is
    the pool an account has left to open against, and isolated buckets are
    locked out of it by being inside that Σ (ADR-0040 §7).
    """
    btc = _position(quantity="0.5", price="58000", side=Side.BUY, symbol="BTC")
    eth = _position(
        quantity="10", price="3000", side=Side.BUY, symbol="ETH", isolated_collateral="6000"
    )

    view = account_view(
        _account("100000"),
        positions=(btc, eth),
        marks={"BTC": Decimal("60000"), "ETH": Decimal("3200")},
        leverage=LeverageBook(entries={"BTC": CROSS_10X, "ETH": ISOLATED_5X}),
        specs={"BTC": BTC_40X, "ETH": ETH_25X},
    )

    assert view.cash == Decimal("100000")
    assert view.equity == Decimal("103000")
    assert view.total_margin_used == Decimal("11000")
    assert view.total_maintenance_margin == Decimal("1015")
    assert view.free_margin == Decimal("92000")
    assert view.effective_leverage is not None
    assert view.effective_leverage.quantize(Decimal("0.0001")) == Decimal("0.6019")


def test_the_account_totals_range_over_symbols_rather_than_partitions() -> None:
    """The Σs are over the **venue's** positions — one per symbol — not over our
    partitions of them (ADR-0035, ADR-0041 §4).

    Two strategies holding offsetting legs is where the two readings come apart,
    because ``notional`` takes a magnitude: the account holds nothing in BTC, so
    its exposure and the collateral behind it are genuinely zero, while a
    per-partition Σ would report ``2 x 110 + 2 x 110 = 440`` of notional and
    ``44`` of margin against a position the venue does not have.

    Each partition's own uPnL is still real and still counts, because that one
    *is* attributable: ``2 x (110 − 100) = +20`` for the long leg and
    ``−2 x (110 − 120) = +20`` for the short, so the account is up 40 on a book
    with no exposure.
    """
    long_leg = _position(quantity="2", price="100", side=Side.BUY)
    short_leg = Position(strategy_id="beta", symbol="BTC")
    short_leg.apply(
        OrderFilled(
            ts_event=1_000,
            ts_init=1_000,
            cloid="0xf9",
            strategy_id="beta",
            signal_id="beta:BTC:1",
            symbol="BTC",
            trade_id="f9",
            quantity=Decimal("2"),
            price=Decimal("120"),
            cum_qty=Decimal("2"),
            fee=Decimal("0"),
        ),
        side=Side.SELL,
    )

    view = account_view(
        _account("100000"),
        positions=(long_leg, short_leg),
        marks={"BTC": Decimal("110")},
        leverage=LeverageBook(entries={"BTC": CROSS_10X}),
        specs={"BTC": BTC_40X},
    )

    assert view.equity == Decimal("100040")
    assert view.total_margin_used == Decimal("0")
    assert view.total_maintenance_margin == Decimal("0")
    assert view.free_margin == Decimal("100040")
    assert view.effective_leverage == Decimal("0")


def test_free_margin_is_reported_when_negative() -> None:
    """A negative free margin is a **valid state, not a divergence** (ADR-0040
    §7), and reporting it is this surface's whole contribution: nothing here
    rejects the order that caused it and nothing liquidates the position.

    That is a deliberate departure from a typical simulator, which refuses the
    margin-breaching order. Ours lets it through and says so — the honest "you
    would have been rejected or liquidated on live" signal.

    On 1000 of cash, ``+0.5`` at 58000 marked to 60000 at **1x** posts the whole
    notional: equity ``1000 + 1000 = 2000`` against ``30000`` of margin used
    leaves ``−28000``. Not clamped at zero, which would report a solvent
    account, and not raised, which would make the report the enforcement.
    """
    view = account_view(
        _account("1000"),
        positions=(_position(quantity="0.5", price="58000", side=Side.BUY),),
        marks={"BTC": Decimal("60000")},
        leverage=LeverageBook(entries={"BTC": CROSS_1X}),
        specs={"BTC": BTC_40X},
    )

    assert view.equity == Decimal("2000")
    assert view.total_margin_used == Decimal("30000")
    assert view.free_margin == Decimal("-28000")
    assert view.effective_leverage == Decimal("15")
