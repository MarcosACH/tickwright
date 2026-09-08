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

from decimal import Decimal

from tickwright.domain import (
    DEFAULT_LEVERAGE,
    AccountView,
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
) -> VenuePositionState:
    return VenuePositionState(
        symbol=symbol,
        signed_size=Decimal(signed_size),
        entry_price=None if entry_price is None else Decimal(entry_price),
        notional=Decimal(notional),
        unrealized_pnl=Decimal(unrealized_pnl),
        margin_used=Decimal("0"),
        isolated_collateral=None,
        liquidation_price=None,
        leverage=DEFAULT_LEVERAGE,
    )


def test_classifies_both_tiers_off_one_hand_built_reading() -> None:
    """The whole classification runs against a reading and a snapshot, nothing else.

    The venue holds 0.5 BTC entered at 100,000 and marked at 120,000, so its
    uPnL is 10,000 and its ``equity`` of 110,000 implies a cash line of exactly
    100,000 (ADR-0040 §7 read backwards). The ledger agrees on cash and equity
    and disagrees on three things chosen to land one finding in each arm this
    cycle compares: a size it is 0.1 short on, a free margin 100 under, and a
    uPnL 2,000 under.

    The uPnL finding is **classified and not alerted**: BTC already carries a
    Tier-1 size finding this pass, and a Tier-2 figure on a grain Tier-1 already
    explains is the same missed fill restated in dollars.
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
        net={"BTC": Decimal("0.4")},
        unrealized={"BTC": Decimal("8000")},
        notional={"BTC": Decimal("60000")},
        mark_observed={"BTC": _NOW_NS},
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
    assert findings.alerts == (findings.divergences[1],)  # the uPnL is Tier-1's to explain
    assert findings.suppressed == 0
    assert findings.unvalued == 0
