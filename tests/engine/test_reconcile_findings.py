"""``ReconcileFindings`` — the account cadence's classification, on its own.

The cycle's pure core: one venue snapshot against one ``LedgerReading``, in and
out with no store, no ``Checkpointer``, no booked fills and no clock beyond the
``now_ns`` handed in. ``tests/engine/test_ledger_reconcile.py`` keeps the whole
cadence — the anchor read, the heal, the records — and reaches these rules only
through a book a real fill moved, which is why three of the band's arms have no
case there: they need a reading a real projection would never produce.

A reading is built here by hand for exactly that reason. It is the same freedom
a recorded venue body already has, pointed at the other side of the comparison.
"""

from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal

import pytest
from closed_sets import assert_covers_exactly

from tickwright.domain import (
    DEFAULT_LEVERAGE,
    AccountView,
    CashCorrection,
    LeverageSpec,
    ReconciliationFill,
    Side,
    SymbolValuation,
    VenueAccountState,
    VenuePositionState,
)
from tickwright.engine.ledger_reconcile import (
    Divergence,
    DivergenceField,
    DivergenceTier,
    ReconcileFindings,
    ValuationBand,
)
from tickwright.engine.portfolio import LedgerReading

_NOW_NS = 1_700_000_000_000_000_000


def _venue(
    *,
    equity: str,
    free_margin: str,
    positions: tuple[VenuePositionState, ...],
) -> VenueAccountState:
    return VenueAccountState(
        equity=Decimal(equity),
        free_margin=Decimal(free_margin),
        cross_maintenance_margin=Decimal("0"),
        as_of_ts_ns=0,
        positions=positions,
    )


def _position(
    symbol: str,
    *,
    signed_size: str,
    entry_price: str | None = "100000",
    notional: str,
    unrealized_pnl: str,
    margin_used: str = "0",
) -> VenuePositionState:
    return VenuePositionState(
        symbol=symbol,
        signed_size=Decimal(signed_size),
        entry_price=None if entry_price is None else Decimal(entry_price),
        notional=Decimal(notional),
        unrealized_pnl=Decimal(unrealized_pnl),
        margin_used=Decimal(margin_used),
        isolated_collateral=None,
        liquidation_price=None,
        leverage=DEFAULT_LEVERAGE,
    )


_CROSS_1X = LeverageSpec(mode="cross", leverage=1)
_CROSS_5X = LeverageSpec(mode="cross", leverage=5)


def _row(
    symbol: str,
    *,
    net: str,
    unrealized_pnl: str | None,
    notional: str | None,
    margin_used: str | None = "0",
    maintenance_margin: str | None = "0",
    leverage: LeverageSpec = DEFAULT_LEVERAGE,
) -> SymbolValuation:
    """One symbol's row of the ledger's side, with the figures a case is not
    about at zero.

    The margin figures default to zero so they agree with the venue double's
    own zeros, and the pair defaults to what a run that configured nothing holds
    every symbol at (ADR-0040 §5). A case whose subject is the mode says so on
    the row, since the classification reads the pair there and nowhere else.
    """
    return SymbolValuation(
        symbol=symbol,
        net=Decimal(net),
        unrealized_pnl=None if unrealized_pnl is None else Decimal(unrealized_pnl),
        notional=None if notional is None else Decimal(notional),
        margin_used=None if margin_used is None else Decimal(margin_used),
        maintenance_margin=None if maintenance_margin is None else Decimal(maintenance_margin),
        leverage=leverage.leverage,
        margin_mode=leverage.mode,
    )


def test_classifies_both_tiers_off_one_hand_built_reading() -> None:
    """The whole classification runs against a reading and a snapshot, nothing else.

    The venue holds 0.5 BTC entered at 100,000 and marked at 120,000, so its
    uPnL is 10,000 and its ``equity`` of 110,000 implies a cash line of exactly
    100,000 (ADR-0040 §7 read backwards). The ledger agrees on cash and equity
    and disagrees on three things chosen to land one finding in each arm this
    cycle compares: a size it is 0.1 short on, a free margin 100 under, and a
    uPnL 2,000 under.

    The two Tier-2 findings are **classified and not alerted**: BTC already
    carries a Tier-1 size finding this pass, and a Tier-2 figure whose compared
    Σ contains BTC is the same missed fill restated in dollars (#322). That is
    BTC's own uPnL, and it is the account's free margin too, since the held
    book is one symbol and BTC is a term of it.
    """
    state = _venue(
        equity="110000",
        free_margin="50000",
        positions=(_position("BTC", signed_size="0.5", notional="60000", unrealized_pnl="10000"),),
    )
    reading = LedgerReading(
        account=AccountView(
            cash=Decimal("100000"),
            equity=Decimal("110000"),
            total_margin_used=Decimal("60000"),
            total_maintenance_margin=Decimal("0"),
            free_margin=Decimal("49900"),
            effective_leverage=None,
        ),
        # The margin figures agree outright with the venue double's own zeros,
        # so the three findings this case is about stay the only ones in the pass.
        rows={"BTC": _row("BTC", net="0.4", unrealized_pnl="8000", notional="60000")},
        mark_observed={"BTC": _NOW_NS},
        last_fills={},
    )

    findings = ReconcileFindings.classify(
        state, reading, band=ValuationBand(), now_ns=_NOW_NS, fills_before=0
    )

    assert findings.divergences == (
        Divergence(
            tier=DivergenceTier.TIER_1,
            field=DivergenceField.SIGNED_SIZE,
            symbol="BTC",
            ledger=Decimal("0.4"),
            venue=Decimal("0.5"),
        ),
        Divergence(
            tier=DivergenceTier.TIER_2,
            field=DivergenceField.FREE_MARGIN,
            symbol=None,
            ledger=Decimal("49900"),
            venue=Decimal("50000"),
        ),
        Divergence(
            tier=DivergenceTier.TIER_2,
            field=DivergenceField.UNREALIZED_PNL,
            symbol="BTC",
            ledger=Decimal("8000"),
            venue=Decimal("10000"),
        ),
    )
    assert findings.alerts == ()  # both Tier-2 figures are Tier-1's to explain
    assert findings.suppressed == 0
    assert findings.unvalued == 0
    # The pass's summary is read off the findings, tier counts included. The
    # record on ``account.reconciled`` is a readout of these six, not a fifth
    # walk over the tuple at the call site.
    assert (findings.tier_1, findings.tier_2) == (1, 2)


def test_a_per_symbol_figure_whose_notional_is_unknown_bands_on_atol_alone() -> None:
    """The band's ``reference=None`` arm, and it falls the **narrow** way.

    ``rtol`` scales by the notional a figure's mark-sensitivity flows through
    (ADR-0046 §5), so with no notional there is no relative term to compute and
    the floor is ``atol`` — the last cent — rather than a tolerance guessed off
    the compared value. Narrow is the conservative direction here: a band that
    cannot be justified alerts instead of staying quiet, which is the opposite
    of every other unknown on this surface, where a figure that cannot be
    computed is dropped. The difference is that a *dropped* figure is counted on
    ``unvalued`` and an unjustified *silence* is counted nowhere.

    One book classified twice, with the notional as the only variable, because
    the assertion is about which floor was used and a single run cannot show
    that: BTC's uPnL is 5 under the venue's, which ``atol`` of 0.01 leaves
    alerted and ``rtol`` against a notional of 60,000 — a floor of 60 — absorbs
    outright.

    Unreachable through the cadence, which is why it is here: the mark whose
    absence makes a notional unknown is the one the uPnL beside it is computed
    from, so a real projection produces both or neither. It stayed unreachable
    through #291 — every field that slice added goes unknown on the predicate
    that makes the notional unknown, or is dropped before the band sees it
    (``ValuationBand.covers``) — so this reading holds a valuation without its
    notional deliberately, and is the only place the arm can be driven at all.

    The account grain is set to agree outright, so the pass has exactly one
    finding and nothing else can account for the alert.
    """
    state = _venue(
        equity="110000",
        free_margin="50000",
        positions=(_position("BTC", signed_size="0.5", notional="60000", unrealized_pnl="10000"),),
    )
    account = AccountView(
        cash=Decimal("100000"),
        equity=Decimal("110000"),
        total_margin_used=Decimal("60000"),
        total_maintenance_margin=Decimal("0"),
        free_margin=Decimal("50000"),
        effective_leverage=None,
    )
    unpriced = LedgerReading(
        account=account,
        rows={"BTC": _row("BTC", net="0.5", unrealized_pnl="9995", notional=None)},
        mark_observed={"BTC": _NOW_NS},
        last_fills={},
    )

    findings = ReconcileFindings.classify(
        state, unpriced, band=ValuationBand(), now_ns=_NOW_NS, fills_before=0
    )

    gap = Divergence(
        tier=DivergenceTier.TIER_2,
        field=DivergenceField.UNREALIZED_PNL,
        symbol="BTC",
        ledger=Decimal("9995"),
        venue=Decimal("10000"),
    )
    assert findings.divergences == (gap,)
    assert findings.alerts == (gap,)
    # The unknown notional is itself a figure this pass could not compare, so it
    # is counted as one — the reference being missing and the *figure* being
    # missing are the same absence read by two rules.
    assert (findings.suppressed, findings.unvalued) == (0, 1)

    priced = LedgerReading(
        account=account,
        rows={"BTC": _row("BTC", net="0.5", unrealized_pnl="9995", notional="60000")},
        mark_observed={"BTC": _NOW_NS},
        last_fills={},
    )

    banded = ReconcileFindings.classify(
        state, priced, band=ValuationBand(), now_ns=_NOW_NS, fills_before=0
    )

    assert banded.divergences == (gap,)  # measured either way — the band gates the alert only
    assert banded.alerts == ()


def test_a_cross_margin_is_banded_against_the_margin_it_posts_not_the_exposure() -> None:
    """Cross ``margin_used`` is ``notional / L``, and so is its band's reference.

    ADR-0046 §5 calls this quantity self-scaling: it is proportional to the
    notional a skew reaches it through, divided by a leverage that is exact
    config rather than anything either side computes. So the reference is the
    posted margin — the value the sensitivity actually flows to — and not the
    exposure above it.

    The distinction is the case. BTC is 60,000 of exposure at 5x, so the venue
    posts 12,000 and the reference is 12,000: a floor of 12. The ledger is 30
    under, which alerts. Referenced against the 60,000 notional instead, the
    floor would be 60 and this same gap would go quiet — a band five times too
    wide, and wider the higher the leverage, which is exactly where a margin gap
    matters most.

    Everything else agrees, so the pass has one finding and nothing to explain
    it away: no Tier-1 grain, no stale mark, no unvalued figure.
    """
    state = _venue(
        equity="110000",
        free_margin="50000",
        positions=(
            _position(
                "BTC",
                signed_size="0.5",
                notional="60000",
                unrealized_pnl="10000",
                margin_used="12000",
            ),
        ),
    )
    reading = LedgerReading(
        account=AccountView(
            cash=Decimal("100000"),
            equity=Decimal("110000"),
            total_margin_used=Decimal("11970"),
            total_maintenance_margin=Decimal("0"),
            free_margin=Decimal("50000"),
            effective_leverage=None,
        ),
        rows={
            "BTC": _row(
                "BTC",
                net="0.5",
                unrealized_pnl="10000",
                notional="60000",
                margin_used="11970",
                leverage=_CROSS_5X,
            )
        },
        mark_observed={"BTC": _NOW_NS},
        last_fills={},
    )

    findings = ReconcileFindings.classify(
        state, reading, band=ValuationBand(), now_ns=_NOW_NS, fills_before=0
    )

    gap = Divergence(
        tier=DivergenceTier.TIER_2,
        field=DivergenceField.MARGIN_USED,
        symbol="BTC",
        ledger=Decimal("11970"),
        venue=Decimal("12000"),
    )
    assert findings.divergences == (gap,)
    assert findings.alerts == (gap,)
    assert (findings.suppressed, findings.unvalued) == (0, 0)

    # The band it cleared, pinned as the arm it is: the same book at 1x posts
    # the whole notional, and the floor of 60 that reference gives absorbs the
    # identical 30. Nothing about the disagreement changed — only the divisor.
    at_1x = replace(state, positions=(replace(state.positions[0], margin_used=Decimal("60000")),))
    unlevered = ReconcileFindings.classify(
        state=at_1x,
        reading=replace(
            reading,
            rows={"BTC": replace(reading.rows["BTC"], margin_used=Decimal("59970"), leverage=1)},
        ),
        band=ValuationBand(),
        now_ns=_NOW_NS,
        fills_before=0,
    )

    assert unlevered.alerts == ()


def test_one_unpriced_symbol_makes_the_account_grains_reference_unknown() -> None:
    """The Σ propagates the unknown, and the alternative is worse than it looks.

    An account-grain figure errs by at most the book's **total** notional — a
    skew reaches ``equity`` and ``free_margin`` through every position at once —
    so with one symbol's notional missing the total is not a smaller total, it
    is unknown. Summing the known terms instead would band the account by the
    fraction of the book that happened to be priced, and narrow it most on the
    largest book, which is the one it least suits: an operator would be woken by
    a gap that is ordinary skew, on the account where skew is biggest.

    Two symbols, and ETH is the unpriced one: the book's free margin is 5 under
    the venue's, which the unknown reference leaves alerted at ``atol`` and the
    complete Σ of 100,000 — a floor of 100 — absorbs. The same book with ETH's
    notional supplied is the second run, so the reference is the only variable.

    Note which way that falls. BTC's 60,000 alone would give a floor of 60 and
    absorb the gap too, so the honest ``None`` is the **noisier** answer here,
    not the quieter one — the case is not "unknown means silence" but "a band we
    cannot justify is not claimed".

    Hand-built for the same reason as the per-symbol case above: a projection
    computes a notional and a uPnL from one mark, so a book where ETH is valued
    but unpriced is not one a cadence can produce.
    """
    state = _venue(
        equity="112000",
        free_margin="50000",
        positions=(
            _position("BTC", signed_size="0.5", notional="60000", unrealized_pnl="10000"),
            _position("ETH", signed_size="10", notional="40000", unrealized_pnl="2000"),
        ),
    )
    account = AccountView(
        cash=Decimal("100000"),
        equity=Decimal("112000"),
        total_margin_used=Decimal("100000"),
        total_maintenance_margin=Decimal("0"),
        free_margin=Decimal("49995"),
        effective_leverage=None,
    )
    unpriced = LedgerReading(
        account=account,
        rows={
            "BTC": _row("BTC", net="0.5", unrealized_pnl="10000", notional="60000"),
            "ETH": _row("ETH", net="10", unrealized_pnl="2000", notional=None),
        },
        mark_observed={"BTC": _NOW_NS, "ETH": _NOW_NS},
        last_fills={},
    )

    findings = ReconcileFindings.classify(
        state, unpriced, band=ValuationBand(), now_ns=_NOW_NS, fills_before=0
    )

    gap = Divergence(
        tier=DivergenceTier.TIER_2,
        field=DivergenceField.FREE_MARGIN,
        symbol=None,
        ledger=Decimal("49995"),
        venue=Decimal("50000"),
    )
    assert findings.divergences == (gap,)
    assert findings.alerts == (gap,)
    # ETH's own notional is a figure the pass could not compare either, counted
    # once beside the account-grain reference it also makes unknown.
    assert (findings.suppressed, findings.unvalued) == (0, 1)

    # ``replace`` rather than a second literal, so the reference is the only
    # variable structurally and not merely by inspection of two blocks.
    priced = replace(
        unpriced,
        rows={**unpriced.rows, "ETH": replace(unpriced.rows["ETH"], notional=Decimal("40000"))},
    )

    banded = ReconcileFindings.classify(
        state, priced, band=ValuationBand(), now_ns=_NOW_NS, fills_before=0
    )

    assert banded.divergences == (gap,)
    assert banded.alerts == ()


def test_a_maintenance_sigma_the_pass_could_not_compute_is_counted_unvalued() -> None:
    """``unvalued``'s one member that can go unknown with every mark in place.

    ``_cross_maintenance`` is ``None`` when any cross-held symbol's rate is
    missing, and a rate is missing because no ``InstrumentSpec`` for the symbol
    reached this run — not because a mark did. So this is the only Tier-2 figure
    whose drop the mark-keyed arm of the count could never see, and the reason
    it is asked for separately rather than inferred from the marks beside it.

    Counted because the count's whole job is to say how many figures went
    unlooked-at (ADR-0011 inv 1), and a maintenance Σ dropped in silence is the
    exact failure it exists to prevent: a live run whose universe lost a symbol's
    rate would report a clean book on every pass, forever, while never once
    comparing the number ADR-0040 §4's tier-crossing alert is computed off.

    The book agrees on everything else — one BTC leg, a fresh mark, and every
    per-symbol figure matching the venue — so the pass's only outcome is the
    count. The row is at cross 1x and not the isolated default, because
    ``_cross_maintenance`` skips isolated symbols and would reach its zero
    rather than the unknown this case is about.
    """
    state = _venue(
        equity="110000",
        free_margin="50000",
        positions=(_position("BTC", signed_size="0.5", notional="60000", unrealized_pnl="10000"),),
    )
    reading = LedgerReading(
        account=AccountView(
            cash=Decimal("100000"),
            equity=Decimal("110000"),
            total_margin_used=Decimal("60000"),
            total_maintenance_margin=None,
            free_margin=Decimal("50000"),
            effective_leverage=None,
        ),
        rows={
            # The absent-spec shape: a rate that never arrived, beside a mark that did.
            "BTC": _row(
                "BTC",
                net="0.5",
                unrealized_pnl="10000",
                notional="60000",
                maintenance_margin=None,
                leverage=_CROSS_1X,
            )
        },
        mark_observed={"BTC": _NOW_NS},
        last_fills={},
    )

    findings = ReconcileFindings.classify(
        state, reading, band=ValuationBand(), now_ns=_NOW_NS, fills_before=0
    )

    assert findings.divergences == ()
    assert findings.alerts == ()
    assert (findings.suppressed, findings.unvalued) == (0, 1)


def test_a_stale_isolated_mark_leaves_the_cross_subset_maintenance_alert_alone() -> None:
    """Staleness silences a Σ only through a term that Σ contains (#305).

    ``maintenance_margin`` is compared over the **cross** subset alone (ADR-0046
    §2.1). An isolated symbol contributes nothing to it, so that symbol's mark
    age says nothing about whether the compared figure is old. Before this fix
    the account grain went stale on any held symbol, which was right for
    ``equity`` and ``free_margin`` (Σs over every position) and wrong here.

    BTC is cross, fresh, and carries a real 100-unit maintenance gap. ETH is
    isolated, five minutes stale, and agrees with the venue on everything. Every
    other figure is built to agree, so the pass has exactly one finding, and the
    isolated mark's age is the only variable between this case and a plain alert.
    The expected outcome is the control run from the issue: one alert, nothing
    suppressed.
    """
    state = replace(
        _venue(
            equity="110000",
            free_margin="50000",
            positions=(
                _position("BTC", signed_size="1", notional="60000", unrealized_pnl="10000"),
                _position("ETH", signed_size="1", notional="40000", unrealized_pnl="0"),
            ),
        ),
        cross_maintenance_margin=Decimal("900"),
    )
    reading = LedgerReading(
        account=AccountView(
            cash=Decimal("100000"),
            equity=Decimal("110000"),
            total_margin_used=Decimal("60000"),
            total_maintenance_margin=Decimal("1500"),
            free_margin=Decimal("50000"),
            effective_leverage=None,
        ),
        rows={
            "BTC": _row(
                "BTC",
                net="1",
                unrealized_pnl="10000",
                notional="60000",
                maintenance_margin="1000",
                leverage=_CROSS_1X,
            ),
            "ETH": _row(
                "ETH", net="1", unrealized_pnl="0", notional="40000", maintenance_margin="500"
            ),
        },
        mark_observed={"BTC": _NOW_NS, "ETH": _NOW_NS - 300 * 10**9},
        last_fills={},
    )

    findings = ReconcileFindings.classify(
        state, reading, band=ValuationBand(), now_ns=_NOW_NS, fills_before=0
    )

    assert [(d.field, d.symbol) for d in findings.divergences] == [
        (DivergenceField.MAINTENANCE_MARGIN, None)
    ]
    assert [(d.field, d.symbol) for d in findings.alerts] == [
        (DivergenceField.MAINTENANCE_MARGIN, None)
    ]
    assert (findings.suppressed, findings.unvalued) == (0, 0)


def test_a_size_finding_on_an_isolated_symbol_leaves_the_cross_subset_maintenance_alert_alone() -> (
    None
):
    """A Tier-1 finding silences a Σ only through a term that Σ contains (#322).

    Rule (1) now flows the way rule (2) does, and this is its narrowing case at
    the classification seam. The case above is the same book under rule (2).
    ``maintenance_margin`` is compared over the cross subset alone (ADR-0046
    §2.1). An isolated symbol is not a term of it, so a size gap on that symbol
    explains nothing about the maintenance figure. A blanket "any size finding
    silences the account grain" would pass the containment case at the top of
    this file and fail here.

    BTC is cross and carries the real 100-unit maintenance gap. ETH is isolated,
    and the venue holds 2 where the ledger holds 1, so ETH's notional gap is the
    same missed fill in dollars and stays silent on its own grain. ETH's uPnL is
    0 on both sides, so the cash, equity and free margin lines still agree. One
    alert, nothing suppressed.
    """
    state = replace(
        _venue(
            equity="110000",
            free_margin="50000",
            positions=(
                _position("BTC", signed_size="1", notional="60000", unrealized_pnl="10000"),
                _position("ETH", signed_size="2", notional="80000", unrealized_pnl="0"),
            ),
        ),
        cross_maintenance_margin=Decimal("900"),
    )
    reading = LedgerReading(
        account=AccountView(
            cash=Decimal("100000"),
            equity=Decimal("110000"),
            total_margin_used=Decimal("60000"),
            total_maintenance_margin=Decimal("1500"),
            free_margin=Decimal("50000"),
            effective_leverage=None,
        ),
        rows={
            "BTC": _row(
                "BTC",
                net="1",
                unrealized_pnl="10000",
                notional="60000",
                maintenance_margin="1000",
                leverage=_CROSS_1X,
            ),
            "ETH": _row(
                "ETH", net="1", unrealized_pnl="0", notional="40000", maintenance_margin="500"
            ),
        },
        mark_observed={"BTC": _NOW_NS, "ETH": _NOW_NS},
        last_fills={},
    )

    findings = ReconcileFindings.classify(
        state, reading, band=ValuationBand(), now_ns=_NOW_NS, fills_before=0
    )

    assert [(d.tier, d.field, d.symbol) for d in findings.divergences] == [
        (DivergenceTier.TIER_1, DivergenceField.SIGNED_SIZE, "ETH"),
        (DivergenceTier.TIER_2, DivergenceField.MAINTENANCE_MARGIN, None),
        (DivergenceTier.TIER_2, DivergenceField.NOTIONAL, "ETH"),
    ]
    assert [(d.field, d.symbol) for d in findings.alerts] == [
        (DivergenceField.MAINTENANCE_MARGIN, None)
    ]
    assert (findings.suppressed, findings.unvalued) == (0, 0)


def _missed_opening_fill() -> VenueAccountState:
    """The venue holds 0.5 BTC the ledger never booked, at cross.

    Entered at 100,000 and marked at 120,000, so its uPnL is 10,000. The
    venue's equity of 110,000 implies a cash line of 100,000, which the ledger
    agrees on. The venue posts 100 of cross maintenance for it.
    """
    return replace(
        _venue(
            equity="110000",
            free_margin="50000",
            positions=(
                _position("BTC", signed_size="0.5", notional="60000", unrealized_pnl="10000"),
            ),
        ),
        cross_maintenance_margin=Decimal("100"),
    )


def _flat_reading(rows: dict[str, SymbolValuation]) -> LedgerReading:
    """A ledger holding nothing: cash, equity and free margin all 100,000."""
    return LedgerReading(
        account=AccountView(
            cash=Decimal("100000"),
            equity=Decimal("100000"),
            total_margin_used=Decimal("0"),
            total_maintenance_margin=Decimal("0"),
            free_margin=Decimal("100000"),
            effective_leverage=None,
        ),
        rows=rows,
        mark_observed=dict.fromkeys(rows, _NOW_NS),
        last_fills={},
    )


def test_a_size_finding_on_a_symbol_the_ledger_never_traded_still_explains_the_account_sums() -> (
    None
):
    """Rule (1) ranges over the Tier-1 symbols, not over what the ledger holds (#322).

    A missed opening fill is a size finding on a symbol the ledger is flat in.
    The ledger's Σ has no BTC term, but the venue's does, and the compared
    figure is the difference between the two. ``equity`` and ``free_margin``
    sum every position on both sides, so BTC is a term of each and the gap is
    the missed fill in dollars.

    ``maintenance_margin`` narrows to the cross subset, and cross-ness is read
    off the ledger's row (#304). A symbol the ledger never traded has no row,
    so the mode cannot be read and the figure is left to alert. That errs
    toward noise, never silence.
    """
    findings = ReconcileFindings.classify(
        _missed_opening_fill(),
        _flat_reading({}),
        band=ValuationBand(),
        now_ns=_NOW_NS,
        fills_before=0,
    )

    assert [(d.tier, d.field, d.symbol) for d in findings.divergences] == [
        (DivergenceTier.TIER_1, DivergenceField.SIGNED_SIZE, "BTC"),
        (DivergenceTier.TIER_2, DivergenceField.EQUITY, None),
        (DivergenceTier.TIER_2, DivergenceField.FREE_MARGIN, None),
        (DivergenceTier.TIER_2, DivergenceField.MAINTENANCE_MARGIN, None),
    ]
    assert [(d.field, d.symbol) for d in findings.alerts] == [
        (DivergenceField.MAINTENANCE_MARGIN, None)
    ]
    assert (findings.suppressed, findings.unvalued) == (0, 0)


def test_a_size_finding_on_a_symbol_closed_to_flat_explains_the_cross_maintenance_too() -> None:
    """The same missed fill on a symbol the ledger traded before (#322).

    A closed position leaves its row behind at zero, and the row still carries
    the mode the run holds the symbol at. BTC is cross, so it is a term of the
    maintenance Σ as well, and nothing alerts.
    """
    rows = {
        "BTC": _row("BTC", net="0", unrealized_pnl="0", notional="0", leverage=_CROSS_1X),
    }

    findings = ReconcileFindings.classify(
        _missed_opening_fill(),
        _flat_reading(rows),
        band=ValuationBand(),
        now_ns=_NOW_NS,
        fills_before=0,
    )

    assert [(d.tier, d.field, d.symbol) for d in findings.divergences] == [
        (DivergenceTier.TIER_1, DivergenceField.SIGNED_SIZE, "BTC"),
        (DivergenceTier.TIER_2, DivergenceField.EQUITY, None),
        (DivergenceTier.TIER_2, DivergenceField.FREE_MARGIN, None),
        (DivergenceTier.TIER_2, DivergenceField.MAINTENANCE_MARGIN, None),
    ]
    assert findings.alerts == ()
    assert (findings.suppressed, findings.unvalued) == (0, 0)


# The per-symbol Tier-2 figures, each with the venue attribute it is read off.
# Module-level so the walk at the end of this file composes it (#303).
_VENUE_POSITION_FIGURES = {
    DivergenceField.UNREALIZED_PNL: "unrealized_pnl",
    DivergenceField.NOTIONAL: "notional",
    DivergenceField.MARGIN_USED: "margin_used",
}

# The account-grain Tier-2 figures, likewise.
_VENUE_ACCOUNT_FIGURES = {
    DivergenceField.EQUITY: "equity",
    DivergenceField.FREE_MARGIN: "free_margin",
    DivergenceField.MAINTENANCE_MARGIN: "cross_maintenance_margin",
}


@pytest.mark.parametrize(("field", "attribute"), _VENUE_POSITION_FIGURES.items())
def test_one_broken_venue_figure_is_one_finding_naming_that_figure(
    field: DivergenceField, attribute: str
) -> None:
    """Each per-symbol figure is read off its own venue attribute and no other (#304).

    The per-symbol compare is one roster of (field, ledger read, venue read)
    triples. A slip that pairs a field with the wrong venue attribute would
    still produce findings, just under another field's name. So the guard is
    isolation: with both sides agreeing on everything, nudge one venue figure
    and expect exactly one per-symbol finding, carrying that figure's name.

    Only the per-symbol grain is asserted. The venue's cash line is implied
    from equity minus open PnL (ADR-0040 §7), so the uPnL nudge also moves an
    account figure, and that is the account grain doing its job.
    """
    position = _position("BTC", signed_size="0.5", notional="60000", unrealized_pnl="10000")
    state = _venue(
        equity="110000",
        free_margin="50000",
        positions=(replace(position, **{attribute: getattr(position, attribute) + Decimal("1")}),),
    )
    reading = LedgerReading(
        account=AccountView(
            cash=Decimal("100000"),
            equity=Decimal("110000"),
            total_margin_used=Decimal("0"),
            total_maintenance_margin=Decimal("0"),
            free_margin=Decimal("50000"),
            effective_leverage=None,
        ),
        rows={"BTC": _row("BTC", net="0.5", unrealized_pnl="10000", notional="60000")},
        mark_observed={"BTC": _NOW_NS},
        last_fills={},
    )

    findings = ReconcileFindings.classify(
        state, reading, band=ValuationBand(), now_ns=_NOW_NS, fills_before=0
    )

    per_symbol = [(d.field, d.symbol) for d in findings.divergences if d.symbol is not None]
    assert per_symbol == [(field, "BTC")]


def _agreeing_book() -> tuple[VenueAccountState, LedgerReading]:
    """One BTC leg both sides agree on, at every figure of both grains."""
    state = _venue(
        equity="110000",
        free_margin="50000",
        positions=(_position("BTC", signed_size="0.5", notional="60000", unrealized_pnl="10000"),),
    )
    reading = LedgerReading(
        account=AccountView(
            cash=Decimal("100000"),
            equity=Decimal("110000"),
            total_margin_used=Decimal("0"),
            total_maintenance_margin=Decimal("0"),
            free_margin=Decimal("50000"),
            effective_leverage=None,
        ),
        rows={"BTC": _row("BTC", net="0.5", unrealized_pnl="10000", notional="60000")},
        mark_observed={"BTC": _NOW_NS},
        last_fills={},
    )
    return state, reading


@pytest.mark.parametrize(("field", "attribute"), _VENUE_ACCOUNT_FIGURES.items())
def test_one_broken_venue_account_figure_is_one_finding_naming_that_figure(
    field: DivergenceField, attribute: str
) -> None:
    """Each account-grain figure is read off its own venue attribute and no other.

    The per-symbol case above, one grain up. The account grain is one roster
    of (field, ledger read, venue read) triples too. Nudge one venue figure on
    an agreeing book and expect exactly one account-grain Tier-2 finding,
    carrying that figure's name.

    Only the Tier-2 account grain is asserted. The venue's cash line is
    implied from equity minus open PnL (ADR-0040 §7), so the equity nudge
    also moves cash, and that is the Tier-1 arm doing its job.
    """
    state, reading = _agreeing_book()
    state = replace(state, **{attribute: getattr(state, attribute) + Decimal("1")})

    findings = ReconcileFindings.classify(
        state, reading, band=ValuationBand(), now_ns=_NOW_NS, fills_before=0
    )

    account_grain = [
        (d.field, d.ledger, d.venue)
        for d in findings.divergences
        if d.symbol is None and d.tier is DivergenceTier.TIER_2
    ]
    assert account_grain == [
        (field, getattr(state, attribute) - Decimal("1"), getattr(state, attribute))
    ]


def test_every_account_figure_the_pass_could_not_compute_counts_once_as_unvalued() -> None:
    """The count ranges over the same roster the classification does.

    All three account-grain figures unknown on one pass: equity and free
    margin waiting on a mark, and the cross maintenance Σ on a rate. None of
    the three is reported, since an unknown is not a disagreement (ADR-0041
    §6), and each is counted once, so the pass says three figures went
    unlooked-at and not one. The per-symbol figures stay known so the count is
    the account grain's alone.
    """
    state, agreeing = _agreeing_book()
    reading = LedgerReading(
        account=replace(agreeing.account, equity=None, free_margin=None),
        rows={
            "BTC": _row(
                "BTC",
                net="0.5",
                unrealized_pnl="10000",
                notional="60000",
                maintenance_margin=None,
                leverage=_CROSS_1X,
            )
        },
        mark_observed=agreeing.mark_observed,
        last_fills={},
    )

    findings = ReconcileFindings.classify(
        state, reading, band=ValuationBand(), now_ns=_NOW_NS, fills_before=0
    )

    assert [d for d in findings.divergences if d.symbol is None] == []
    assert findings.unvalued == 3


def _fee_taken_reading(*, net: str, last_fills: dict[str, int]) -> LedgerReading:
    """The ledger after a fill the venue body was serialised before.

    Cash is 10 under the venue's implied 100,000, the fee that fill paid, and
    the size is whatever the case says. uPnL agrees, so equity is under by the
    same 10 and stays explained by the cash finding.
    """
    return LedgerReading(
        account=AccountView(
            cash=Decimal("99990"),
            equity=Decimal("109990"),
            total_margin_used=Decimal("60000"),
            total_maintenance_margin=Decimal("0"),
            free_margin=Decimal("49990"),
            effective_leverage=None,
        ),
        rows={"BTC": _row("BTC", net=net, unrealized_pnl="10000", notional="60000")},
        mark_observed={"BTC": _NOW_NS},
        last_fills=last_fills,
    )


def test_a_pass_with_a_fill_inside_the_read_reports_both_tier_1_findings_and_heals_neither() -> (
    None
):
    """The heal plan is decided where the findings are, and the read window
    defers both halves of it (#284, #330).

    The ledger's fill count stood at 2 before the venue read. The reading taken
    after says BTC's last fill was the third, so a fill landed inside the read.
    The size gap and the cash gap are both reported, neither is healed, and the
    pass says how many findings it held back rather than leaving an operator to
    read a deferred pass as a healed one.
    """
    state = _missed_opening_fill()
    reading = _fee_taken_reading(net="0.4", last_fills={"BTC": 3})

    findings = ReconcileFindings.classify(
        state, reading, band=ValuationBand(), now_ns=_NOW_NS, fills_before=2
    )

    tier_1 = [(d.field, d.symbol) for d in findings.divergences if d.tier is DivergenceTier.TIER_1]
    assert tier_1 == [(DivergenceField.CASH, None), (DivergenceField.SIGNED_SIZE, "BTC")]
    assert findings.heals == ()
    assert findings.cash is None
    assert findings.deferred == 2
    assert findings.unpriced == 0


def test_a_quiet_pass_plans_both_heals_paired_with_their_findings_on_one_stamp() -> None:
    """The same book with no fill inside the read heals both gaps.

    The fill count stood at 2 before the read and BTC's last fill was the
    second, so nothing moved. The size heal buys the 0.1 the ledger is short,
    at the venue's own entry price, and the cash correction assigns the venue's
    100,000. Each rides next to the finding it closes, and both carry the one
    instant the classification was made at, so a retried pass mints the same
    keys for both halves.
    """
    state = _missed_opening_fill()
    reading = _fee_taken_reading(net="0.4", last_fills={"BTC": 2})

    findings = ReconcileFindings.classify(
        state, reading, band=ValuationBand(), now_ns=_NOW_NS, fills_before=2
    )

    (heal,) = findings.heals
    assert heal.divergence == Divergence(
        tier=DivergenceTier.TIER_1,
        field=DivergenceField.SIGNED_SIZE,
        symbol="BTC",
        ledger=Decimal("0.4"),
        venue=Decimal("0.5"),
    )
    assert heal.fill == ReconciliationFill(
        symbol="BTC",
        side=Side.BUY,
        quantity=Decimal("0.1"),
        price=Decimal("100000"),
        ts_ns=_NOW_NS,
    )
    assert findings.cash is not None
    assert findings.cash.divergence == Divergence(
        tier=DivergenceTier.TIER_1,
        field=DivergenceField.CASH,
        symbol=None,
        ledger=Decimal("99990"),
        venue=Decimal("100000"),
    )
    assert findings.cash.correction == CashCorrection(target=Decimal("100000"), ts_ns=_NOW_NS)
    assert (findings.deferred, findings.unpriced) == (0, 0)


def test_a_size_the_venue_posts_no_price_for_is_counted_unpriced_and_not_healed() -> None:
    """A closing heal has no price to book at, and it says so apart from the read window.

    The ledger holds 0.4 BTC the venue no longer carries, so the venue posts
    no entry price for it. Nothing moved during the read. The finding is
    reported, not healed, and counted as ``unpriced`` rather than
    ``deferred``: a deferral clears on the next pass, and this one stays until
    the venue prices the symbol again. Mixed into one count, a stuck symbol
    would read like a busy one.
    """
    state = _venue(equity="100000", free_margin="100000", positions=())
    reading = LedgerReading(
        account=AccountView(
            cash=Decimal("100000"),
            equity=Decimal("100000"),
            total_margin_used=Decimal("40000"),
            total_maintenance_margin=Decimal("0"),
            free_margin=Decimal("60000"),
            effective_leverage=None,
        ),
        rows={"BTC": _row("BTC", net="0.4", unrealized_pnl="0", notional="40000")},
        mark_observed={"BTC": _NOW_NS},
        last_fills={"BTC": 2},
    )

    findings = ReconcileFindings.classify(
        state, reading, band=ValuationBand(), now_ns=_NOW_NS, fills_before=2
    )

    tier_1 = [(d.field, d.symbol) for d in findings.divergences if d.tier is DivergenceTier.TIER_1]
    assert tier_1 == [(DivergenceField.SIGNED_SIZE, "BTC")]
    assert findings.heals == ()
    assert findings.cash is None
    assert (findings.deferred, findings.unpriced) == (0, 1)


_Book = tuple[VenueAccountState, LedgerReading]


def _venue_position_off_by_one(attribute: str) -> Callable[[_Book], _Book]:
    def nudge(book: _Book) -> _Book:
        state, reading = book
        (position,) = state.positions
        moved = replace(position, **{attribute: getattr(position, attribute) + Decimal("1")})
        return replace(state, positions=(moved,)), reading

    return nudge


def _venue_account_off_by_one(attribute: str) -> Callable[[_Book], _Book]:
    def nudge(book: _Book) -> _Book:
        state, reading = book
        return replace(state, **{attribute: getattr(state, attribute) + Decimal("1")}), reading

    return nudge


def _ledger_cash_off_by_one(book: _Book) -> _Book:
    state, reading = book
    account = replace(reading.account, cash=reading.account.cash + Decimal("1"))
    return state, replace(reading, account=account)


def _ledger_size_off_by_a_tenth(book: _Book) -> _Book:
    state, reading = book
    (row,) = reading.rows.values()
    return state, replace(reading, rows={row.symbol: replace(row, net=row.net - Decimal("0.1"))})


# One agreeing book, nudged so that the named figure disagrees. The Tier-1
# pair is nudged on the ledger's side, since the venue's cash is implied and
# its size is what the ledger is measured against. The six Tier-2 rows are
# the isolation tests' own rosters, so a row deleted there is a member with
# no producer here.
_PRODUCED_BY: dict[DivergenceField, Callable[[_Book], _Book]] = {
    DivergenceField.CASH: _ledger_cash_off_by_one,
    DivergenceField.SIGNED_SIZE: _ledger_size_off_by_a_tenth,
    **{f: _venue_position_off_by_one(a) for f, a in _VENUE_POSITION_FIGURES.items()},
    **{f: _venue_account_off_by_one(a) for f, a in _VENUE_ACCOUNT_FIGURES.items()},
}


@pytest.mark.parametrize("field", list(DivergenceField), ids=lambda f: f.value)
def test_every_divergence_field_is_produced_by_a_classifier(field: DivergenceField) -> None:
    """The closed vocabulary is walked, member by member (#303).

    ``DivergenceField`` argues its own closedness on the ground that a member
    nothing answers for fails silently. It did, once: #194 landed the band over
    three of six Tier-2 figures, and ``notional``, ``margin_used`` and the
    account ``maintenance_margin`` were classified nowhere for a whole slice
    with every check green. So this is the catalog walk's shape, pointed at
    the enum: one producer per member, the pass run over it, and the member
    found among what the pass measured.

    Member-grained. It sees that a member is produced and never whether
    ``_reference`` scales it by the right notional. A member that falls into
    the account-grain default by accident is still green here, and that
    clause stays a reviewer's.
    """
    state, reading = _PRODUCED_BY[field](_agreeing_book())

    findings = ReconcileFindings.classify(
        state, reading, band=ValuationBand(), now_ns=_NOW_NS, fills_before=0
    )

    assert field in {d.field for d in findings.divergences}


def test_the_walk_names_every_divergence_field() -> None:
    assert_covers_exactly(DivergenceField, _PRODUCED_BY, what="_PRODUCED_BY")
