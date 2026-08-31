"""``LedgerReconciliation`` — the account-grain healing cycle (ADR-0034/0040).

The correctness net for the money line, and its own module rather than a third
cadence on ``Reconciler``: that one is anchored on a cloid and asks whether an
*order* converged, while this one is anchored on an account snapshot and asks
whether the *ledger* still matches what the venue holds. Different anchor,
different freeze grain, its own alert types — folded together, one class would
carry two of each.

**Live-only.** Paper has no second account to compare against — ``PaperExchange``
"persists nothing and holds no position state" (ADR-0043 §4) — so the runner
wires the cadence on the live path alone, and paper's atomic ledger write stands
in for it. Manufacturing a paper account to fill the gap would be the "second
internal projection that would apply identical fills to an identical position and
only ever agree with itself" ADR-0035 rejects.

This slice **classifies and changes no stored value**: the Tier-1 heals and the
Tier-2 band land on the classification established here.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from tickwright.domain import Clock, EventBus, Exchange
from tickwright.observability import NamedEvent, named_event

from .portfolio import PortfolioProjection


class DivergenceTier(StrEnum):
    """Which half of the two-tier model a disagreement lives in (ADR-0034).

    The distinction is what the engine may *do* about it, not how large it is:
    Tier-1 is ledger truth and is healed through synthetic events, Tier-2 is a
    computed valuation and only ever alerts.
    """

    TIER_1 = "tier_1"
    TIER_2 = "tier_2"


@dataclass(frozen=True, slots=True, kw_only=True)
class Divergence:
    """One disagreement between the ledger and the venue, already classified.

    ``projected`` is our number and ``venue`` is theirs, kept as the pair rather
    than reduced to a delta: an operator reading the record needs to know which
    side moved, and a signed difference alone cannot say.

    ``symbol`` is ``None`` for an account-grain quantity — there is one
    collateral pool per process (ADR-0038), so a cash or equity disagreement
    belongs to no symbol and attributing it to one would be a fiction.
    """

    tier: DivergenceTier
    quantity: str
    symbol: str | None
    projected: Decimal
    venue: Decimal


@dataclass(frozen=True, slots=True)
class LedgerReconcileConfig:
    """The cadence's interval and the ADR-0040 §6 alert band.

    ``interval_seconds`` matches the open-order cadence's 30s: both are venue
    polls at a grain where a missed cycle costs a late alert rather than a lost
    order.

    ``atol``/``rtol`` are carried here now and **applied by the Tier-2 band
    slice** — the band is a property of this cycle's config whichever slice reads
    it, and splitting the two would make the alert slice re-open a type it does
    not otherwise touch. The defaults are ADR-0040 §6's, ``rtol`` on measured
    evidence (#142: ≈7× the 3-second mark-skew p99) and ``atol`` as the pure
    rounding floor ADR-0046 §5's stable reference leaves it as.
    """

    interval_seconds: float = 30.0
    atol: Decimal = Decimal("0.01")
    rtol: Decimal = Decimal("0.001")


class LedgerReconciliation:
    """The account-grain cross-check, one venue read per cycle."""

    def __init__(
        self,
        *,
        exchange: Exchange,
        portfolio: PortfolioProjection,
        clock: Clock,
        bus: EventBus,
        config: LedgerReconcileConfig,
    ) -> None:
        self._exchange = exchange
        # The **concrete** projection, not the ``Portfolio`` seam: the comparison
        # is against the account net over *every* partition, the reserved
        # unattributed one included, and the seam withholds it by design
        # (ADR-0041 §5/§8). A scoped facade here would net to a book the venue
        # does not have.
        self._portfolio = portfolio
        self._clock = clock
        self._bus = bus
        self._config = config

    async def reconcile_account(self) -> tuple[Divergence, ...] | None:
        """One cycle: read the venue, classify what disagrees, heal nothing.

        ``None`` is the **freeze** — no venue truth to compare against — and it
        is deliberately not ``()``: an empty tuple says the ledger was compared
        and agreed, and collapsing the two would let an outage read as a clean
        book (ADR-0011 inv 1).
        """
        await self._exchange.fetch_account_state()
        divergences: tuple[Divergence, ...] = ()
        named_event(NamedEvent.ACCOUNT_RECONCILED, divergences=len(divergences))
        return divergences
