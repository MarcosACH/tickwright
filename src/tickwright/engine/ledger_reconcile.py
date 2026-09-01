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

from tickwright.domain import Exchange
from tickwright.observability import NamedEvent, named_event

from .portfolio import PortfolioProjection


class LedgerReconciliation:
    """The cross-check between the ledger and the venue's own account truth."""

    def __init__(self, *, exchange: Exchange, portfolio: PortfolioProjection) -> None:
        self._exchange = exchange
        self._portfolio = portfolio

    async def reconcile_account(self) -> tuple[()] | None:
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
        named_event(NamedEvent.ACCOUNT_RECONCILED)
        return ()
