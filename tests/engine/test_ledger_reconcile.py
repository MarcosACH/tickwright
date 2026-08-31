"""``LedgerReconciliation`` — the account-grain cross-check (ADR-0034/0040).

The live-only cadence anchored on one ``fetch_account_state()`` read: it
classifies what the ledger and the venue disagree about, by tier, and **changes
no stored value** in this slice. The heals land on the classification it
establishes.

Wired the way the runner wires it — a real ``PortfolioProjection`` over a real
``SQLiteStore``, a ``ManualClock``, an ``InMemoryBus`` — against a venue double
answering with recorded venue figures, which is the only half a test may invent:
the point of the cross-check is that the second number comes from somewhere the
projection did not compute it.
"""

import asyncio
from decimal import Decimal

from ledgers import book_fill
from venue_doubles import DERIVED_STATE, LIVE_ACCOUNT_ID, LiveVenueDouble

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    AccountSpec,
    MarkTick,
    OrderFilled,
    OrderFillEvent,
    PlaceOrder,
    Side,
    VenueOrderView,
    VenueReadFailure,
)
from tickwright.engine.ledger_reconcile import LedgerReconcileConfig, LedgerReconciliation
from tickwright.engine.portfolio import PortfolioProjection
from tickwright.observability.testing import capture_events

_ENTRY = "64809.0"
"""The recorded snapshot's own entry price — see ``venue_doubles.account_state``."""

_AGREEING_MARK = "64792.0"
"""The mark at which the projection's uPnL on 0.002 BTC long reads the venue's
own −0.034: ``(64792.0 − 64809.0) × 0.002``. Derived from the recorded figures
rather than from the code under test, so the Tier-2 legs have an independent
expected value."""


def _live_ledger(store: SQLiteStore, clock: ManualClock) -> PortfolioProjection:
    """A ledger on the **live** shape — an ingested genesis, so the opening cash
    line is the venue's number and not a declaration a test chose."""
    return PortfolioProjection(
        spec=AccountSpec(account_id=LIVE_ACCOUNT_ID, genesis_collateral=None),
        store=store,
        clock=clock,
    )


def _fill(*, quantity: str, price: str, symbol: str = "BTC") -> OrderFillEvent:
    return OrderFilled(
        ts_event=1_000,
        ts_init=1_000,
        cloid=f"0x{symbol}-{quantity}",
        strategy_id="alpha",
        signal_id=f"alpha:{symbol}:1",
        symbol=symbol,
        trade_id=f"t-{symbol}-{quantity}",
        quantity=Decimal(quantity),
        price=Decimal(price),
        cum_qty=Decimal(quantity),
        fee=Decimal("0"),
    )


def _mark(price: str, *, symbol: str = "BTC") -> MarkTick:
    return MarkTick(ts_event=2_000, ts_init=2_000, symbol=symbol, price=Decimal(price))


def _agreeing_ledger(store: SQLiteStore, clock: ManualClock) -> PortfolioProjection:
    """A ledger holding exactly what ``DERIVED_STATE`` reports: 0.002 BTC long
    off an ingested genesis, marked so its uPnL matches the venue's."""
    projection = _live_ledger(store, clock)
    projection.materialise(DERIVED_STATE)
    book_fill(projection, _fill(quantity="0.002", price=_ENTRY), side=Side.BUY)
    projection.observe_mark(_mark(_AGREEING_MARK))
    return projection


class _ReadOnlyVenue(LiveVenueDouble):
    """A live venue the account cycle may only *read*.

    The three order-path members carry this suite's meaning by refusing: the
    cycle's whole contract this slice is that it classifies and heals nothing, so
    a placement reaching the venue is the failure, not an unimplemented member.
    """

    async def place(self, order: PlaceOrder) -> None:
        raise AssertionError("the account cycle places nothing: it classifies, it never heals")

    async def cancel(self, cloid: str) -> None:
        raise AssertionError("the account cycle cancels nothing: it classifies, it never heals")

    async def fetch_order(self, cloid: str) -> VenueOrderView | VenueReadFailure:
        raise AssertionError("the account cycle is anchored on the account read, never on a cloid")


def _cycle(
    projection: PortfolioProjection, venue: _ReadOnlyVenue, clock: ManualClock
) -> LedgerReconciliation:
    return LedgerReconciliation(
        exchange=venue,
        portfolio=projection,
        clock=clock,
        bus=InMemoryBus(),
        config=LedgerReconcileConfig(),
    )


def test_a_cycle_agreeing_with_the_venue_reports_no_divergence() -> None:
    """The tracer: one venue read, nothing to classify, and a record saying so.

    ``()`` is not ``None`` — an agreeing cycle and a frozen one are different
    outcomes, and collapsing them would let an outage read as a clean book."""
    clock = ManualClock(start_ns=1_000)
    store = SQLiteStore(":memory:")
    venue = _ReadOnlyVenue()
    reconciliation = _cycle(_agreeing_ledger(store, clock), venue, clock)

    with capture_events() as logs:
        divergences = asyncio.run(reconciliation.reconcile_account())

    assert divergences == ()
    assert venue.account_reads == 1
    assert [str(log["event"]) for log in logs] == ["account.reconciled"]
