"""``LedgerReconciliation`` — the account grain's healing cycle (ADR-0034).

Its own module rather than a third cadence on ``Reconciler``: that one is
anchored on a **cloid** and asks whether an order converged, this one is
anchored on an **account snapshot** and asks whether the ledger still matches
what the venue holds. Different anchor, different freeze grain, its own alert
types — folded together, one class would carry two of each.

**Live only.** Paper has no second account to compare against — ``PaperExchange``
persists nothing and holds no position state (ADR-0043 §4) — and manufacturing
one would be the second internal projection ADR-0035 rejects, agreeing only ever
with itself. What paper has in its place is the atomic ledger write.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from tickwright.domain import Exchange, VenueAccountState, venue_cash
from tickwright.observability import NamedEvent, named_event

from .portfolio import PortfolioProjection

_ZERO = Decimal("0")


class DivergenceTier(Enum):
    """Which of ADR-0034's two tiers a disagreement was found at.

    The tier is the whole of what classification decides, because it is what
    every later response turns on: Tier-1 is *accumulated* state — a gap there
    compounds into every future number, so it carries zero economic tolerance
    and is healed through a synthetic event — while Tier-2 is *recomputed on
    every read* and can only ever be alerted on, inside a band. Carrying the
    tier on the record rather than in the shape of two collections keeps that
    single decision in one place for the two slices that act on it.
    """

    TIER_1 = "tier-1"
    TIER_2 = "tier-2"


@dataclass(frozen=True, slots=True, kw_only=True)
class Divergence:
    """One figure on which the ledger and the venue's snapshot disagree.

    Both sides are carried, never their difference: the heal that lands next
    needs the venue's number as its **target** (ADR-0034 makes the venue
    authoritative), and an operator reading the record needs to see the pair to
    tell a missed fill from a duplicated one. A delta alone answers neither.

    ``symbol`` is ``None`` for a figure held at the account grain, where the
    account has one collateral pool and no symbol to attribute it to.
    """

    tier: DivergenceTier
    field: str
    symbol: str | None
    ledger: Decimal
    venue: Decimal


class LedgerReconciliation:
    """The cross-check between the ledger and the venue's own account truth."""

    def __init__(self, *, exchange: Exchange, portfolio: PortfolioProjection) -> None:
        self._exchange = exchange
        self._portfolio = portfolio

    async def reconcile_account(self) -> tuple[Divergence, ...] | None:
        """One cycle: read the venue account once, classify what disagrees.

        The single ``fetch_account_state`` read is the anchor and the whole
        cycle's venue cost (ADR-0034) — the account snapshot carries every
        symbol, so nothing here polls per symbol.

        ``None`` is a **frozen** cycle: the read failed, so there is nothing to
        reconcile against and nothing may be inferred from its absence — least
        of all a flat book (ADR-0011 inv 1). An empty tuple is the opposite
        answer, a book that agreed, and the two are deliberately not collapsed
        into one. The freeze costs this cycle alone; the next deadline reads
        again.
        """
        state = await self._exchange.fetch_account_state()
        if state is None:
            named_event(NamedEvent.ACCOUNT_RECONCILE_FROZEN)
            return None
        divergences = self._cash(state) + self._sizes(state)
        named_event(NamedEvent.ACCOUNT_RECONCILED)
        return divergences

    def _cash(self, state: VenueAccountState) -> tuple[Divergence, ...]:
        """Tier-1: the accumulated cash line against the one the snapshot implies.

        The venue publishes no cash line, so the comparison is against
        ``venue_cash(state)`` — ADR-0040 §7's ``equity = cash + Σ uPnL`` read
        backwards, and the *same* function the genesis was ingested through, so
        the line an account opened at and the line it is checked against can
        never be two derivations.

        Account grain, so the record carries no symbol: the account has one
        collateral pool and open PnL is not attributable to it. Ahead of the
        per-symbol findings because that is the order the two are read in — the
        pool first, then what it is backing.
        """
        ledger = self._portfolio.account().cash
        venue = venue_cash(state)
        if ledger == venue:
            return ()
        return (
            Divergence(
                tier=DivergenceTier.TIER_1,
                field="cash",
                symbol=None,
                ledger=ledger,
                venue=venue,
            ),
        )

    def _sizes(self, state: VenueAccountState) -> tuple[Divergence, ...]:
        """Tier-1: the account-net signed size per symbol against the venue's.

        Ranged over the **union** of both symbol sets, with an absent side
        reading flat. Either half alone is a check that cannot see the direction
        it is not looking in: comparing only what the venue returned misses a
        position the ledger believes it holds and the venue has closed, and
        comparing only what the ledger knows about misses flow the engine never
        placed (ADR-0038's unattributed partition), which the account is
        nonetheless carrying margin for. A symbol traded to flat sits in the
        ledger's half at zero and agrees with a venue that omits it, so the
        union costs nothing on the ordinary book.

        Exact equality is the whole tolerance — Tier-1 accumulates, so any gap
        is a missed or duplicated fill rather than noise (ADR-0034). Sorted by
        symbol so a cycle's report is a function of the book and not of dict
        iteration order.
        """
        ledger = self._portfolio.account_net()
        venue = {position.symbol: position.signed_size for position in state.positions}
        return tuple(
            Divergence(
                tier=DivergenceTier.TIER_1,
                field="signed_size",
                symbol=symbol,
                ledger=ledger.get(symbol, _ZERO),
                venue=venue.get(symbol, _ZERO),
            )
            for symbol in sorted(ledger.keys() | venue.keys())
            if ledger.get(symbol, _ZERO) != venue.get(symbol, _ZERO)
        )
