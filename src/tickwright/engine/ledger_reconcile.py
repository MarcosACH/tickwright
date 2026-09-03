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

from tickwright.domain import (
    AccountAnchor,
    AccountModeVerdict,
    AccountView,
    CashCorrection,
    ReconciliationFill,
    Side,
    VenueAccountState,
    venue_cash,
)
from tickwright.observability import NamedEvent, named_event

from .checkpoint import Checkpointer
from .portfolio import HealChange

_ZERO = Decimal("0")


class _FreezeCaller(Enum):
    """Which of the account anchor's two readers a failed read froze — the
    ``scope`` field on ``account.reconcile_frozen``.

    One name covers both because one anchor failing one way is one failure, and
    a second event name would make an operator learn two vocabularies for it.
    What the two do not share is the **cost**: ``CADENCE`` loses this pass and
    the next deadline reads again, while ``BARRIER`` spends the startup budget
    and then faults the process (``invariants.md`` inv 1). A field rather than a
    second name is the call ``reconcile.frozen`` already makes for its own two
    scopes — the catalog is closed (ADR-0020/0045) and nothing routes on the
    difference, but plenty reads it.
    """

    BARRIER = "barrier"
    CADENCE = "cadence"


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


class DivergenceField(Enum):
    """Which figure of the account snapshot a disagreement was found on.

    Closed rather than a free string, and for the same reason ``tier`` beside it
    is: the two slices that consume the classification both **branch** on it —
    #178 heals a cash line and a size through different synthetic events, and
    #194 bands a Tier-2 figure against the notional its own mark-sensitivity
    flows through — so a name is a case label, not a caption. Left as literals
    across four construction sites here and every branch there, a typo is a case
    that silently never fires, on a cadence whose whole job is to notice what
    nothing else would.

    The account grain's two figures carry no symbol; the per-symbol two do
    (``Divergence.symbol``), which is the other half of what a consumer keys on.
    """

    CASH = "cash"
    SIGNED_SIZE = "signed_size"
    EQUITY = "equity"
    UNREALIZED_PNL = "unrealized_pnl"


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
    field: DivergenceField
    symbol: str | None
    ledger: Decimal
    venue: Decimal


@dataclass(frozen=True, slots=True, kw_only=True)
class _SizeHeal:
    """A Tier-1 size finding and the synthetic fill built to close it.

    The pair is carried from the moment it exists rather than rebuilt at the
    announcement, because the two are only relatable by the predicate that
    produced them: ``_size_heals`` decides which findings heal, and anything
    downstream re-deriving that decision is a second statement of it that can
    drift. The announcement needs both halves — the ``event_id`` off the
    synthetic, and the ledger/venue pair off the finding, since a fill carries
    only its delta and the pair is what an operator reads.
    """

    divergence: Divergence
    fill: ReconciliationFill


@dataclass(frozen=True, slots=True, kw_only=True)
class _CashHeal:
    """The Tier-1 cash finding and the correction built to close it.

    ``_SizeHeal``'s account-grain sibling, and there is at most one per pass:
    the account has one collateral pool (ADR-0041 §2).
    """

    divergence: Divergence
    correction: CashCorrection


def _healed(divergence: Divergence, *, event_id: str) -> None:
    """Record the correction that closed ``divergence``, under the key it was
    applied with.

    Decimals are stringified for the reason every other record's are: a figure
    an operator greps for has to render identically wherever the line lands,
    and a ``Decimal`` is at the renderer's mercy on the way out.
    """
    named_event(
        NamedEvent.ACCOUNT_HEALED,
        field=divergence.field.value,
        symbol=divergence.symbol,
        ledger=str(divergence.ledger),
        venue=str(divergence.venue),
        event_id=event_id,
    )


class LedgerReconciliation:
    """The cross-check between the ledger and the venue's own account truth."""

    def __init__(self, *, exchange: AccountAnchor, checkpointer: Checkpointer) -> None:
        # The **account** anchor and not the whole ``Exchange``: one snapshot
        # read and the mode guard on it are the only venue members this cycle
        # touches, so the constructor states that it cannot place an order —
        # a claim the class docstring used to have to make in prose.
        self._exchange = exchange
        # The ``Checkpointer`` rather than the projection it lends, because this
        # cycle **writes**: a Tier-1 heal is one fold, one durable write and one
        # projection, in that order and with no yield between them — the exact
        # ordering ADR-0043 §4 gives that type to own. Taking the read-model
        # alone and reaching for a ``Store`` beside it would be the second store
        # parameter the ``Checkpointer`` exists to make unwireable.
        self._checkpointer = checkpointer
        self._portfolio = checkpointer.portfolio

    async def materialise_account(self) -> bool:
        """The startup barrier's live-only first step: create the account row
        when the store holds none (ADR-0042 §6, ADR-0043 §6).

        Here rather than on the runner because this is the account grain's other
        venue read, and the grain now has an owner. The barrier composes two
        bound methods on the two components that own what each step proves —
        this one and ``Reconciler.reconcile_startup`` — so the runner keeps the
        *ordering* and nothing else, which is the only part of the barrier that
        is genuinely lifecycle.

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
        move a recorded number no later check could ever contradict, and a rule
        that lived only in this method would be one the next caller of that verb
        inherits nothing from. The two read the same predicate off the same
        store, so they cannot disagree about the row.

        ``False`` is a failed venue read, and the barrier retries it inside the
        one startup budget before faulting. Clearing the barrier on an account
        the venue never answered for is not an available outcome: that is
        ADR-0011's freeze-don't-guess applied to the cash line, and what keeps
        ADR-0041 §6's "``cash`` is never ``None``" true rather than intended.
        It is recorded under the same name the cadence's freeze uses — one
        anchor, one failure, one vocabulary — and it earns the record more,
        since this freeze faults the process where that one loses a pass.

        The write itself is the projection's rather than a ``Checkpointer``
        verb, for the same reason paper's genesis seed is: the ``Checkpointer``
        owns the orderings a caller could silently invert — fold before write
        before project, ledger before order cache — and opening a ledger is one
        write to one read-model with no ordering inside it.
        """
        if self._portfolio.is_opened():
            return True
        state = await self._exchange.fetch_account_state()
        if state is None:
            self._freeze(_FreezeCaller.BARRIER)
            return False
        self._portfolio.materialise(state)
        return True

    @staticmethod
    def _freeze(caller: _FreezeCaller) -> None:
        """The account anchor came back empty: record it and infer nothing."""
        named_event(NamedEvent.ACCOUNT_RECONCILE_FROZEN, scope=caller.value)

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

        The ledger's side is read **once** for the whole cycle, as the venue's
        is: the account view, the account-net sizes and the per-symbol uPnL map
        are each a fold over every partition, so taking them here rather than
        inside each check both halves the folds and gives the comparison one
        reading per side. That is the property ``domain.valuation`` states about
        assembling a view in one call — two fields of one view can never
        straddle a fill — kept by the cycle rather than left to the checks being
        synchronous. Which is now load-bearing rather than tidy: the heal below
        moves the cash line, and ``equity`` is ``cash + Σ uPnL``, so a view taken
        again after it would report the venue as disagreeing by exactly the
        amount this cycle just moved — an alert about our own arithmetic, and one
        no Tier-1 equity finding exists to suppress it against.

        One thing does come between that reading and the write, and only on a
        pass with a cash gap to close: the mode guard's venue read. What a
        concurrent fill costs there, and why the answer is the next deadline
        rather than a second reading, is ``_mode_verified`` below.

        The net sizes are the reason that matters beyond tidiness: they are the
        cycle's **one definition of held-ness**, and both grains read it. Tier-1
        calls a symbol flat and calls it absent the same thing, so Tier-2 must
        too, or a symbol traded back to flat that the venue still reports is
        counted as held by one check and unheld by the other — and the single
        missed fill is reported twice, once as size and once as valuation.

        The pass is recorded with **what it found**, not merely that it ran: the
        heal and the alert land in later slices, so until they do this record is
        the classification's only reader. ``unvalued`` is the third outcome the
        counts would otherwise hide — a Tier-2 figure the ledger cannot compute
        is dropped rather than reported (ADR-0041 §6), which is correct and
        leaves a pass that never looked indistinguishable from one that agreed.
        """
        state = await self._exchange.fetch_account_state()
        if state is None:
            self._freeze(_FreezeCaller.CADENCE)
            return None
        # The pull is also the channel ADR-0039 kept alive for the one Tier-2
        # figure live reads through instead of computing (ADR-0040 §3), so the
        # snapshot is handed to the projection before anything is compared
        # against it — a cycle that classified first would leave the cadence's
        # own reader one pass behind the read it just made. It is not a
        # divergence input: we report the venue's own number, so there is
        # nothing here to diverge against.
        self._portfolio.observe_venue_liquidation(state)
        view = self._portfolio.account()
        net = self._portfolio.account_net()
        unrealized = self._portfolio.account_unrealized()
        divergences = (
            self._cash(state, cash=view.cash)
            + self._sizes(state, ledger=net)
            + self._equity(state, equity=view.equity)
            + self._unrealized(state, ledger=unrealized, net=net)
        )
        # One stamp for the pass, spent on both halves of its heal: the
        # deterministic key is the *cycle's*, so a size heal and the cash
        # correction beside it are one retryable unit rather than two the clock
        # could separate.
        heal_ts_ns = self._checkpointer.clock.timestamp_ns()
        heals = self._size_heals(state, divergences, ts_ns=heal_ts_ns)
        cash = self._cash_heal(divergences, ts_ns=heal_ts_ns)
        if cash is not None and not await self._mode_verified():
            cash = None
        booked = self._checkpointer.checkpoint_heal(
            tuple(heal.fill for heal in heals),
            cash=None if cash is None else cash.correction,
            # The locked buckets ride the same snapshot and the same transaction
            # (ADR-0043 §3). Not a divergence input: live never computes this
            # number — the venue posts it, and ``updateIsolatedMargin`` is an
            # action this engine does not model — so there is one figure rather
            # than two, and nothing to compare. What the band eventually watches
            # is the ``margin_used`` computed *from* it (ADR-0040 §6).
            collateral={
                position.symbol: position.isolated_collateral for position in state.positions
            },
        )
        self._record_heals(heals, cash=cash, booked=booked)
        tiers = [divergence.tier for divergence in divergences]
        named_event(
            NamedEvent.ACCOUNT_RECONCILED,
            tier_1=tiers.count(DivergenceTier.TIER_1),
            tier_2=tiers.count(DivergenceTier.TIER_2),
            unvalued=self._unvalued(state, view=view, ledger=unrealized, net=net),
        )
        return divergences

    async def _mode_verified(self) -> bool:
        """Whether the venue still reports a mode this cycle may heal cash
        toward (ADR-0046 §4), recording the refusal when it does not.

        Asked **only when there is a cash heal to make**, which is the whole of
        why this check is affordable: ``userAbstraction`` is weight 20 against
        the anchor read's 2, so a per-cycle guard would spend ten times on the
        watchman what it spends on the thing watched, forever, to catch an event
        that takes a deliberate master-wallet action. On the divergence path it
        costs nothing in steady state and sits on the code path that does the
        damage rather than on a timer hoping to reach it first. A mode switch
        that produces no cash divergence is empty as a concern — the switch *is*
        a re-pooling of balances.

        Both non-verified verdicts take this branch, because an unverified mode
        is not evidence of an unchanged one: proceeding would heal on exactly
        the assumption the guard exists to stop the engine making. They differ
        only on the record, which is where an operator reads them.

        A **freeze and not a fault**, and the caller's shape says so — the cash
        correction is dropped and the pass runs on. Our fills are still our
        fills and every Tier-2 number is computed from ``(position, mark)``
        rather than from the venue, so what has become invalid is the
        cross-check and the heal, not the ledger.

        This is also the cycle's **one yield point between classifying and
        writing**, and it is worth naming because the caller's own docstring
        reasons about a ledger side read once for the pass. The cadence runs
        beside the saga in the runner's ``TaskGroup``, so a fill can be
        checkpointed while this read is in flight, and the correction booked
        after it was computed against the book as it was before: the cash line is
        an assignment to a target, so that fill's cash effect is overwritten and
        the size heals are deltas against a net that has since moved.

        Accepted rather than closed, on ADR-0034's own terms: the ledger
        over-reads for **one cadence interval** and heals back, because the next
        snapshot carries the fill and the exact cash comparison finds the gap it
        left. Closing it is not available at this grain anyway — the venue
        snapshot predates the await too, so re-reading the ledger side would heal
        toward the same figure against a book the venue has not seen, and the
        only sound response to "a fill landed" is the pass this one already is:
        one the next deadline repeats. The window is a round-trip on a path that
        only runs when something diverged, and it costs nothing on the far commoner
        pass that finds no cash gap and never opens it.
        """
        verdict = await self._exchange.verify_account_mode()
        if verdict is AccountModeVerdict.VERIFIED:
            return True
        named_event(NamedEvent.ACCOUNT_MODE_UNVERIFIED, reason=verdict.value)
        return False

    @staticmethod
    def _record_heals(
        heals: tuple[_SizeHeal, ...],
        *,
        cash: _CashHeal | None,
        booked: HealChange | None,
    ) -> None:
        """One ``account.healed`` per correction this pass actually booked.

        Driven off what the **fold kept**, never off the findings and never off
        the synthetics alone: a Tier-1 divergence the cycle declined to heal — a
        size it could not price — is reported on the pass's own record and has
        moved nothing, so a record here would answer an operator's "why did the
        ledger move" with a move that never happened.

        A synthetic the aggregate *refused* is the same claim reached through the
        one door the producer-side checks do not cover. ``_size_heals`` can only
        decline to build a fill; whether the fill it built moved anything is
        ``Position.apply``'s answer, and on a retried pass it is no — the key is
        the cycle's stamp, so the second run of one pass mints the first's
        ``event_id`` and is deduped as a redelivery. ``apply_heal`` reports that
        verdict as the set of keys it kept, so this reads a membership rather
        than pairing its own input against the fold's output by position. The
        cash correction is not in it and needs no such check: it is an
        assignment against a line the same pass found unequal, so it always
        moved.

        The figures come back off the ``Divergence`` the synthetic was built
        from rather than off the synthetic itself, because a fill carries only
        its delta and the pair is what an operator reads: the same reason the
        finding carries both sides instead of their difference. The pair arrives
        already made (``_SizeHeal``, ``_CashHeal``) — a heal is announced against
        the finding it was built to close, not against one matched back to it
        here.

        Emitted **after** the checkpoint, so nothing is announced that the
        transaction could still refuse to keep.
        """
        kept = frozenset[str]() if booked is None else booked.applied
        for heal in heals:
            if heal.fill.event_id in kept:
                _healed(heal.divergence, event_id=heal.fill.event_id)
        if cash is not None:
            _healed(cash.divergence, event_id=cash.correction.event_id)

    @staticmethod
    def _cash_heal(divergences: tuple[Divergence, ...], *, ts_ns: int) -> _CashHeal | None:
        """This pass's Tier-1 cash finding as the correction that closes it.

        The target is the divergence's own venue side, so the figure the cycle
        heals to is the figure it classified against — read again here, it could
        be a second derivation of ``venue_cash`` off the same snapshot, and the
        two disagreeing would heal toward a number the pass never reported.

        ``None`` when the line agrees, which is what keeps an agreeing pass a
        read: ``_cash`` reports on exact inequality, so a finding here always has
        a real gap behind it.

        There is at most one — the account has one collateral pool (ADR-0041 §2)
        — and the search says so rather than assuming it: a second would mean
        ``_cash`` had grown a grain, and silently healing to whichever came first
        is the failure mode a ``next`` hides.

        The one-element unpack is that statement and is **deliberately not** an
        ``InvariantViolation``, on the idiom ``exchange.py``'s adjudicators
        already use: a raise here would be a branch no input through this seam
        can reach, since ``_cash`` compares one ledger figure against one venue
        figure and can only ever emit one finding. The two would fault the run
        identically anyway — the cadence catches nothing, so either aborts the
        ``TaskGroup`` — and what a bare unpack costs by comparison is a line of
        diagnosis, against an untestable line of code and a hole in this
        module's coverage. If ``_cash`` ever does grow a grain, the rule to
        restate is this one, not the exception type.
        """
        found = [
            divergence
            for divergence in divergences
            if divergence.tier is DivergenceTier.TIER_1 and divergence.field is DivergenceField.CASH
        ]
        if not found:
            return None
        (divergence,) = found
        return _CashHeal(
            divergence=divergence,
            correction=CashCorrection(target=divergence.venue, ts_ns=ts_ns),
        )

    @staticmethod
    def _size_heals(
        state: VenueAccountState, divergences: tuple[Divergence, ...], *, ts_ns: int
    ) -> tuple[_SizeHeal, ...]:
        """Turn this pass's Tier-1 size findings into the fills that correct them.

        Each fill goes back **paired with the finding it closes** (``_SizeHeal``)
        rather than alone. This is the one place that decides which findings heal
        at all, and the announcement needs both halves; returning the fills bare
        would leave the pairing to be reconstructed downstream off a second
        statement of this predicate.

        The delta is ``venue − ledger`` and the venue is authoritative
        (ADR-0034), so the fill moves the ledger *to* the snapshot rather than
        by some fraction of the way. Its magnitude and side come apart because
        ``Position`` keeps a magnitude and takes a direction; a short heal is a
        sell of the absolute gap.

        **The price is the venue's own entry price**, and a symbol that has none
        is not healed this pass. ADR-0034's synthetic needs a price to book
        against, and there is no honest substitute for one: a size booked at an
        invented price would put a real number on ``entry_price`` and make every
        valuation derived from it wrong in a way no later cycle re-examines,
        where an unhealed symbol simply diverges again at the next deadline and
        stays visible as a finding. The venue omits the field on a position it
        does not carry, so this is also what a *closing* heal falls under.

        Classification runs before this, off the cycle's one fold, so a heal can
        never feed the comparison that produced it — the Tier-2 findings on this
        pass are about the book the cycle *found*, not the one it left behind.

        The zero-delta arm is **unreachable from this pass's own input** and kept
        anyway: ``_sizes`` reports on exact inequality, so a size finding it
        emitted always has a gap. What it guards is the producer side of the rule
        — a heal of zero is not a fact worth putting on the path, and the
        alternative to refusing it here is a well-formed synthetic that
        ``Position.apply``'s magnitude precondition faults the run over after the
        saga has already moved. That makes it structurally untestable through
        this seam, which is why the suite pins the priceless finding above
        instead: same claim, reachable input.

        ``ts_ns`` is the cycle's, handed in beside the cash correction's rather
        than read here: both halves of one pass's heal are keyed on one stamp.
        """
        prices = {position.symbol: position.entry_price for position in state.positions}
        return tuple(
            _SizeHeal(
                divergence=divergence,
                fill=ReconciliationFill(
                    symbol=divergence.symbol,
                    side=Side.BUY if delta > _ZERO else Side.SELL,
                    quantity=abs(delta),
                    price=price,
                    ts_ns=ts_ns,
                ),
            )
            for divergence in divergences
            if divergence.tier is DivergenceTier.TIER_1
            and divergence.field is DivergenceField.SIGNED_SIZE
            and divergence.symbol is not None
            and (price := prices.get(divergence.symbol)) is not None
            and (delta := divergence.venue - divergence.ledger) != _ZERO
        )

    @staticmethod
    def _holds(net: dict[str, Decimal], symbol: str) -> bool:
        """Whether the ledger carries exposure in ``symbol`` — the cycle's one
        held-ness predicate, read by both Tier-2 checks.

        Flat and absent are the **same** answer, which is the definition Tier-1
        already works to: ``_sizes`` ranges over the union of both symbol sets
        with a missing side reading zero, so a symbol traded back to flat and a
        symbol never traded are one state there. A closed position leaves its
        record behind at zero, so reading presence-in-the-map as held instead
        would make every symbol this engine has ever closed a held one.
        """
        return net.get(symbol, _ZERO) != _ZERO

    @classmethod
    def _unvalued(
        cls,
        state: VenueAccountState,
        *,
        view: AccountView,
        ledger: dict[str, Decimal | None],
        net: dict[str, Decimal],
    ) -> int:
        """How many Tier-2 figures this pass could not compute at all.

        The account's equity, plus one per symbol **both** sides hold whose
        ledger valuation is waiting on a mark — the same ``_holds`` range
        ``_unrealized`` classifies over, since a symbol only one side carries is
        already a Tier-1 size finding rather than a missing valuation. A symbol
        the ledger holds flat, or does not carry at all, reads as not held and
        so is never unvalued: nothing was going to value it.
        """
        absent_marks = sum(
            1
            for position in state.positions
            if cls._holds(net, position.symbol) and ledger.get(position.symbol) is None
        )
        return absent_marks + (1 if view.equity is None else 0)

    def _cash(self, state: VenueAccountState, *, cash: Decimal) -> tuple[Divergence, ...]:
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

        ``cash`` is handed in off the cycle's one account view rather than read
        here, so this check and ``_equity`` compare against the same reading.
        """
        venue = venue_cash(state)
        if cash == venue:
            return ()
        return (
            Divergence(
                tier=DivergenceTier.TIER_1,
                field=DivergenceField.CASH,
                symbol=None,
                ledger=cash,
                venue=venue,
            ),
        )

    def _sizes(
        self, state: VenueAccountState, *, ledger: dict[str, Decimal]
    ) -> tuple[Divergence, ...]:
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

        ``ledger`` is the cycle's one net fold, handed in rather than taken
        here: it is also what ``_holds`` reads, so the grain that decides a
        symbol is flat and the grain that decides it is unheld cannot be looking
        at two folds.
        """
        venue = {position.symbol: position.signed_size for position in state.positions}
        return tuple(
            Divergence(
                tier=DivergenceTier.TIER_1,
                field=DivergenceField.SIGNED_SIZE,
                symbol=symbol,
                ledger=ledger.get(symbol, _ZERO),
                venue=venue.get(symbol, _ZERO),
            )
            for symbol in sorted(ledger.keys() | venue.keys())
            if ledger.get(symbol, _ZERO) != venue.get(symbol, _ZERO)
        )

    def _equity(
        self, state: VenueAccountState, *, equity: Decimal | None
    ) -> tuple[Divergence, ...]:
        """Tier-2: the recomputed account equity against the venue's own.

        Un-banded, deliberately. ADR-0040 §6's tolerance lands with the alert
        slice, and what this cycle owes it is a classified pair to apply a
        tolerance *to* — a difference dropped here is one no band was ever
        asked about.

        A ``None`` equity is **not a divergence**: it means one held symbol has
        no mark, so the Σ is uncomputable rather than wrong (ADR-0041 §6), and
        reporting the absence as a disagreement would alert on our own missing
        input while claiming the venue's number is at fault. Dropped from the
        findings, it is still counted on the cycle's record (``_unvalued``), so
        the pass that could not look is not read as the pass that agreed.
        """
        if equity is None or equity == state.equity:
            return ()
        return (
            Divergence(
                tier=DivergenceTier.TIER_2,
                field=DivergenceField.EQUITY,
                symbol=None,
                ledger=equity,
                venue=state.equity,
            ),
        )

    def _unrealized(
        self,
        state: VenueAccountState,
        *,
        ledger: dict[str, Decimal | None],
        net: dict[str, Decimal],
    ) -> tuple[Divergence, ...]:
        """Tier-2: per-symbol open PnL, at the account grain both sides hold it.

        Ranged over the symbols **both** sides hold — ``_holds`` against the
        venue's own roster — where the Tier-1 checks range over the union, and
        the asymmetry is the point. A symbol only one side carries has already
        been reported as a size divergence, and its uPnL gap is that same
        disagreement restated in another unit rather than a second finding: the
        valuation is not wrong, the book is. Reporting it twice would hand the
        alert slice a Tier-2 record whose only honest response is to suppress it.

        Held-ness is the ledger's **net**, never presence in the uPnL map: a
        closed position leaves its record behind, valuing flat at a real zero
        (``domain.valuation``'s per-term exemption), so a symbol traded back to
        flat would otherwise read as held here while reading as absent at Tier-1
        — one missed fill, reported once as size and once as valuation.

        The ledger's side is the Σ over every partition of the symbol, because
        the venue holds one position per symbol and a partition's own slice
        would be a fraction compared against a whole (ADR-0041 §4). ``None``
        is skipped for the reason equity's is: a valuation waiting on a mark is
        unknown, not divergent — and counted for the same reason too.

        Both maps are the cycle's one fold each, handed in rather than taken
        here so the classification, the size check and the ``unvalued`` count
        all read the same two readings.
        """
        return tuple(
            Divergence(
                tier=DivergenceTier.TIER_2,
                field=DivergenceField.UNREALIZED_PNL,
                symbol=position.symbol,
                ledger=held,
                venue=position.unrealized_pnl,
            )
            for position in sorted(state.positions, key=lambda p: p.symbol)
            if self._holds(net, position.symbol)
            and (held := ledger.get(position.symbol)) is not None
            and held != position.unrealized_pnl
        )
