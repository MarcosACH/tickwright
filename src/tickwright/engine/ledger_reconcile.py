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

from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum, StrEnum

from tickwright.domain import Clock, EventBus, Exchange, VenueAccountState, venue_cash
from tickwright.observability import NamedEvent, named_event

from .portfolio import PortfolioProjection

_FLAT = Decimal(0)
"""What a symbol the venue does not report is held at.

Legitimately zero rather than unknown, and only because the read **succeeded**:
a successful account read enumerates every position the account holds, so a
symbol absent from it is one the venue is flat in. The unanswered read never
reaches here — it is the ``None`` the cycle freezes on (ADR-0011 inv 1)."""


class _FreezeStep(Enum):
    """Which account-grain reader an unanswered venue read froze — the ``step``
    field on ``account.reconcile_frozen``.

    ``BARRIER`` is the startup materialisation and ``CADENCE`` the running
    cross-check. A field rather than a second event name, following
    ``reconcile.frozen``'s ``scope``: the grain and the cause are identical and
    nothing routes on the difference, but the **cost** differs sharply — the
    barrier's ``None`` faults the process after its retry budget, the cadence's
    skips one cycle — and an operator needs that told apart.
    """

    BARRIER = "barrier"
    CADENCE = "cadence"


class DivergenceTier(StrEnum):
    """Which half of the two-tier model a disagreement lives in (ADR-0034).

    The distinction is what the engine may *do* about it, not how large it is:
    Tier-1 is ledger truth and is healed through synthetic events, Tier-2 is a
    computed valuation and only ever alerts.
    """

    TIER_1 = "tier_1"
    TIER_2 = "tier_2"


class DivergenceQuantity(StrEnum):
    """Which figure disagreed — the closed set this cycle ever compares.

    A `StrEnum` for the same reason ``tier`` is one rather than the bare `str`
    a fifth call site could still spell as ``"unrealised_pnl"`` and type-check:
    the four members are exhaustive over ``_compare_sizes``/``_compare_cash``/
    ``_compare_valuations``, so a later slice reading this field has a closed
    set to match against, not a convention to remember.
    """

    SIGNED_SIZE = "signed_size"
    CASH = "cash"
    EQUITY = "equity"
    UNREALIZED_PNL = "unrealized_pnl"


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
    quantity: DivergenceQuantity
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

    async def materialise_account(self) -> bool:
        """The barrier's live-only first step: create the account row when the
        store holds none (ADR-0042 §6, ADR-0043 §6).

        A method on this type rather than on the runner, and that is the #191
        handover's first item: the barrier composes two bound methods on two
        grain owners, and until this module existed the account step had no owner
        to be a method of. Its collaborators are exactly this class's — the
        ``Exchange`` it reads, the ``PortfolioProjection`` it writes through — so
        the runner is left holding lifecycle ordering and nothing else.

        The predicate is the *row*, not the venue: paper reaches here already
        opened, seeded inside ``recover()`` from a config value that could not
        fail on connectivity, and a live restart reaches here opened by an
        earlier life. So the one state that reads the venue is a live **first**
        start, and both of the other two skip the read entirely rather than
        making one they would then discard.

        What that check decides here is only whether a **read is owed**, not
        whether the write is allowed. ADR-0042 §3's write-once rule is stated on
        ``materialise`` itself, which refuses an already-open ledger (ADR-0047
        §1): on live the genesis is *provenance only* — nothing cross-checks it,
        because there is no configured counterpart — so a second derivation would
        move a recorded number no later check could ever contradict. The two read
        the same predicate off the same store, so they cannot disagree about the
        row.

        ``False`` is a failed venue read, and the barrier retries it inside the
        one startup budget before faulting. Clearing the barrier on an account
        the venue never answered for is not an available outcome: that is
        ADR-0011's freeze-don't-guess applied to the cash line, and what keeps
        ADR-0041 §6's "``cash`` is never ``None``" true rather than intended.

        The write itself is the projection's rather than a ``Checkpointer``
        verb, for the same reason paper's genesis seed is: the ``Checkpointer``
        owns the orderings a caller could silently invert — fold before write
        before project, ledger before order cache — and opening a ledger is one
        write to one read-model with no ordering inside it. The ordering that
        *does* matter here is the barrier's, and it stays with the runner.
        """
        if self._portfolio.is_opened():
            return True
        state = await self._exchange.fetch_account_state()
        if state is None:
            self._freeze(_FreezeStep.BARRIER)
            return False
        self._portfolio.materialise(state)
        return True

    async def reconcile_account(self) -> tuple[Divergence, ...] | None:
        """One cycle: read the venue, classify what disagrees, heal nothing.

        ``None`` is the **freeze** — no venue truth to compare against — and it
        is deliberately not ``()``: an empty tuple says the ledger was compared
        and agreed, and collapsing the two would let an outage read as a clean
        book (ADR-0011 inv 1).
        """
        state = await self._exchange.fetch_account_state()
        if state is None:
            self._freeze(_FreezeStep.CADENCE)
            return None
        divergences = (
            *self._compare_sizes(state),
            *self._compare_cash(state),
            *self._compare_valuations(state),
        )
        named_event(NamedEvent.ACCOUNT_RECONCILED, divergences=len(divergences))
        return divergences

    def _freeze(self, step: _FreezeStep) -> None:
        """Record the account grain's connectivity guard tripping (ADR-0011 inv 1).

        The record is the whole of what a freeze *does*: nothing is classified,
        nothing is healed, nothing is removed. Emitted rather than left silent
        because the alternative — a step that touches nothing and says nothing —
        reads exactly like a cadence that stopped running, and at the barrier
        left an operator with ``engine.faulted`` and no record naming the read
        that caused it.
        """
        named_event(NamedEvent.ACCOUNT_RECONCILE_FROZEN, step=step.value)

    def _compare_valuations(self, state: VenueAccountState) -> Iterator[Divergence]:
        """The computed half: account equity, and per-symbol unrealized PnL.

        Reported **un-banded** in this slice — every disagreement is yielded,
        however small. The ADR-0040 §6 band and its two suppressions filter this
        set in the alert slice; classification is what establishes the set.

        An uncomputable number is **not compared** rather than compared as zero.
        A ``None`` equity or Σ means a held position has no mark, so the ledger
        is not what the two sides would disagree about (ADR-0041 §6).
        """
        equity = self._portfolio.account().equity
        if equity is not None and equity != state.equity:
            yield Divergence(
                tier=DivergenceTier.TIER_2,
                quantity=DivergenceQuantity.EQUITY,
                symbol=None,
                projected=equity,
                venue=state.equity,
            )
        venue_pnl = {position.symbol: position.unrealized_pnl for position in state.positions}
        for symbol, projected in self._portfolio.account_unrealized_pnl().items():
            # Only symbols the venue reports: one we hold and it does not has
            # already been caught as a Tier-1 phantom, and a Tier-2 alert on the
            # valuation of a position that should not exist is noise on top of a
            # heal (ADR-0040 §6's first suppression, in structural form).
            if projected is None or symbol not in venue_pnl:
                continue
            if projected != venue_pnl[symbol]:
                yield Divergence(
                    tier=DivergenceTier.TIER_2,
                    quantity=DivergenceQuantity.UNREALIZED_PNL,
                    symbol=symbol,
                    projected=projected,
                    venue=venue_pnl[symbol],
                )

    def _compare_cash(self, state: VenueAccountState) -> Iterator[Divergence]:
        """The account grain's Tier-1 leg — one collateral pool, so no symbol.

        The venue publishes no cash field, so its side is **derived**:
        ``venue_cash`` is the same ``equity − Σ unrealized_pnl`` a live ledger's
        genesis was opened at (ADR-0042 §6), read from ``domain`` rather than
        restated here so the two can never disagree.
        """
        projected = self._portfolio.account().cash
        venue = venue_cash(state)
        if projected != venue:
            yield Divergence(
                tier=DivergenceTier.TIER_1,
                quantity=DivergenceQuantity.CASH,
                symbol=None,
                projected=projected,
                venue=venue,
            )

    def _compare_sizes(self, state: VenueAccountState) -> Iterator[Divergence]:
        """The Σ-invariant's venue link, per symbol (ADR-0034).

        Compared at the **account** grain on both sides: our net folds every
        partition, the reserved unattributed one included, against the venue's
        one position per symbol. Per-strategy attribution is never reconciled —
        the venue has no per-strategy truth to reconcile it against.

        The symbol set is the **union**, and neither side may bound it. Ours
        alone would miss foreign flow — a position the venue holds that this
        engine never placed, which is exactly the drift the venue link exists to
        catch; the venue's alone would miss a phantom our ledger is carrying
        against a symbol the venue is flat in.
        """
        venue_sizes = {position.symbol: position.signed_size for position in state.positions}
        projected_sizes = self._portfolio.account_net()
        for symbol in {**projected_sizes, **venue_sizes}:
            projected = projected_sizes.get(symbol, _FLAT)
            venue = venue_sizes.get(symbol, _FLAT)
            if projected != venue:
                yield Divergence(
                    tier=DivergenceTier.TIER_1,
                    quantity=DivergenceQuantity.SIGNED_SIZE,
                    symbol=symbol,
                    projected=projected,
                    venue=venue,
                )
