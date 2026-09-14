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

from dataclasses import replace
from decimal import Decimal

import pytest

from tickwright.domain import (
    DEFAULT_LEVERAGE,
    AccountView,
    LeverageSpec,
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

    findings = ReconcileFindings.classify(state, reading, band=ValuationBand(), now_ns=_NOW_NS)

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

    findings = ReconcileFindings.classify(state, unpriced, band=ValuationBand(), now_ns=_NOW_NS)

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

    banded = ReconcileFindings.classify(state, priced, band=ValuationBand(), now_ns=_NOW_NS)

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

    findings = ReconcileFindings.classify(state, reading, band=ValuationBand(), now_ns=_NOW_NS)

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

    findings = ReconcileFindings.classify(state, unpriced, band=ValuationBand(), now_ns=_NOW_NS)

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

    banded = ReconcileFindings.classify(state, priced, band=ValuationBand(), now_ns=_NOW_NS)

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

    findings = ReconcileFindings.classify(state, reading, band=ValuationBand(), now_ns=_NOW_NS)

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

    findings = ReconcileFindings.classify(state, reading, band=ValuationBand(), now_ns=_NOW_NS)

    assert [(d.field, d.symbol) for d in findings.divergences] == [
        (DivergenceField.MAINTENANCE_MARGIN, None)
    ]
    assert [(d.field, d.symbol) for d in findings.alerts] == [
        (DivergenceField.MAINTENANCE_MARGIN, None)
    ]
    assert (findings.suppressed, findings.unvalued) == (0, 0)


@pytest.mark.parametrize(
    ("field", "attribute"),
    [
        (DivergenceField.UNREALIZED_PNL, "unrealized_pnl"),
        (DivergenceField.NOTIONAL, "notional"),
        (DivergenceField.MARGIN_USED, "margin_used"),
    ],
)
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

    findings = ReconcileFindings.classify(state, reading, band=ValuationBand(), now_ns=_NOW_NS)

    per_symbol = [(d.field, d.symbol) for d in findings.divergences if d.symbol is not None]
    assert per_symbol == [(field, "BTC")]
