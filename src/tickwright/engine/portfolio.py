"""``PortfolioProjection`` — the write-through projection of position and
account state (ADR-0035): the economic sibling of the order ``Cache``.

It is a **separate** projection from the ``Cache``, not a widening of it —
different key, different rows, different venue anchor — and, crucially, it is
**not a bus subscriber** for fills. Tier-1 is written synchronously on the
``ExecutionManager``'s fill-apply path, so the projection is the fill's *writer*
and applies it exactly once; a strategy reading its position from the
``OrderFilled`` handler reads the state that fill just produced (ADR-0045 §1).

The concrete carries a **wider read surface than the ``Portfolio`` seam** — every
partition including the reserved unattributed one — because reconciliation,
telemetry and the CLI read this class directly and a strategy must never make
those reads (ADR-0041 §8). The scoped facade ``for_strategy`` hands out is what
implements the seam.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from tickwright.domain import (
    Account,
    AccountSpec,
    AccountView,
    Clock,
    FundingAccrual,
    InvariantViolation,
    LeverageSpec,
    MarkTick,
    OrderFillEvent,
    Portfolio,
    Position,
    PositionChange,
    PositionView,
    Side,
    Store,
    StoreAccountMismatch,
    VenueAccountState,
    account_net_size,
    account_view,
    position_view,
)
from tickwright.observability import NamedEvent, named_event

_ZERO = Decimal("0")

# What a fill did to a position → its cataloged name (ADR-0045 §2). A position
# change is never a bus event — it is an output derived from a fill already on
# the bus — so the named-event catalog is the only place it is observable from
# outside, and a ``PositionChange`` missing from this map fails with a KeyError.
_POSITION_EVENTS: dict[PositionChange, NamedEvent] = {
    PositionChange.OPENED: NamedEvent.POSITION_OPENED,
    PositionChange.CHANGED: NamedEvent.POSITION_CHANGED,
    PositionChange.CLOSED: NamedEvent.POSITION_CLOSED,
}


def _mismatch_report(disagreements: list[tuple[str, str, str]]) -> str:
    """Lay every disagreeing field out as a two-column diff (ADR-0042 §3).

    One line per field, both sides on it, remedy last — because two mismatches
    read as a sentence are something an operator has to parse, and read as a diff
    are something they can act on. The columns are padded to the widest entry for
    the same reason: what matters is which side of which row changed.

    The **store path** ADR-0042 §3's example also names is deliberately absent,
    from the remedy as well as from the opening line. The check holds a ``Store``,
    not a file — a path or a DSN is the adapter's own fact (ADR-0019) — and the
    operator already knows which store they pointed the run at. What only the
    engine can tell them is which fields disagreed.
    """
    label_width = max(len(label) for label, _, _ in disagreements) + 1
    stored_width = max(len(stored) for _, stored, _ in disagreements)
    rows = "\n".join(
        f"  {label + ':':<{label_width}} stored {stored:<{stored_width}}  "
        f"config declares {configured}"
        for label, stored, configured in disagreements
    )
    return (
        "the durable ledger belongs to a different account.\n"
        f"{rows}\n"
        "A different genesis is a different account history. Point the run at a "
        "fresh store to open a new ledger, or restore the declared values to "
        "resume this one."
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class LedgerChange:
    """One fill's effect on the ledger: folded in memory, not yet durable.

    The atomic path's hand-off (ADR-0043 §4). Making a fill durable is three
    steps that cannot collapse into one call — fold the aggregates, write them
    with the order row in a single transaction, *then* make the result readable
    — because the write takes the *folded* state as its input, so the fold
    cannot follow the write. This carries the middle state across: ``apply_fill``
    returns it, the ``ExecutionManager`` checkpoints it, ``project`` publishes it.

    ``account`` is the projection's own aggregate rather than a copy: cash is
    one pool per process (ADR-0038), so there is nothing to hand over but the
    reference. ``position`` is a live reference too — both aggregates are
    mutable and ``apply_fill`` has already advanced them, so a partition that
    has traded before reads its new size the moment the fold returns. What the
    split defers is what a reader could otherwise reach *first*: a partition's
    **first** filing — before it, ``position()`` reports ``None`` — and the
    ``position.*`` announcement. Keeping the rest off a reader is the
    ``ExecutionManager``'s doing rather than this type's: it holds no ``await``
    between the fold and the write, the same no-yield discipline ``Store``
    documents for a checkpoint.

    A *change*, deliberately not an *entry*: the ledger persists as mutable
    current-state rows, and ADR-0043 §1 reserves an append-only line-item log as
    an extension point it does not take. "Entry" is that log's word, so it would
    name this the row of a table that does not exist.
    """

    account: Account
    position: Position
    changes: tuple[PositionChange, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class FundingChange:
    """One accrual's effect on the ledger: folded in memory, not yet durable.

    ``LedgerChange``'s sibling on the path with no carrier fill, and the shape
    differs exactly where the two facts differ. It carries **many** positions
    rather than one, because a symbol's accrual is split across every partition
    holding it; it carries no ``changes``, because an accrual moves a record
    without ever opening or closing one, so the announcement behind the write is
    ``position.changed`` for every partition here and nothing else to choose
    between (ADR-0045 §2); and it carries the **watermark advance**, which is the
    whole reason the pair exists — the mark must land in the same transaction as
    the funding line it guards, so it travels with that line rather than
    following it (ADR-0043 §5.2).

    ``funding_mark`` is a ``(symbol, boundary_ts)`` pair, at the grain the key it
    is the durable half of: one value per symbol, deliberately not a column on
    any of the position rows beside it here.
    """

    account: Account
    positions: tuple[Position, ...]
    funding_mark: tuple[str, int]


class PortfolioProjection:
    """The one owner of "what do I hold, and what has it earned"."""

    def __init__(
        self,
        *,
        spec: AccountSpec,
        store: Store,
        clock: Clock,
        leverage: Mapping[str, LeverageSpec] | None = None,
    ) -> None:
        # The venue's declaration, not just the account opened from it: it is
        # what tells the recovery path whether this run's genesis was *declared*
        # (paper) or is *ingested* (live, ``None``), which is the predicate the
        # genesis seed turns on (ADR-0043 §10).
        self._spec = spec
        self._store = store
        self._clock = clock
        self._account = Account.open(spec, ts_ns=clock.timestamp_ns())
        # Keyed ``(strategy_id, symbol)`` — the materialized runtime key of the
        # logical ``(account, strategy, symbol)`` grain, the account being a
        # deployment fact (ADR-0038). ``None`` is the reserved unattributed
        # partition, reachable here but never through the seam.
        self._positions: dict[tuple[str | None, str], Position] = {}
        # The Tier-2 half, and the asymmetry with the line above is the model:
        # accumulated ledger state is ordering-critical and so is written
        # synchronously on the fill path, while a latest-value cache is not —
        # a slightly-late mark is a slightly-stale valuation, which Tier-2
        # tolerates by construction, being alert-only and never healed
        # (ADR-0039). So this half arrives by plain bus subscription.
        self._marks: dict[str, MarkTick] = {}
        # The **resolved** per-symbol map (ADR-0044 §2), complete over the
        # strategy-traded set and identical to the one the ``Exchange`` holds,
        # because the composition root resolved it once and injected it twice.
        # The margin model reads it from here; this slice lands the input alone.
        self._leverage = dict(leverage or {})

    def leverage_for(self, symbol: str) -> LeverageSpec:
        """The margin mode and leverage this run computes ``symbol`` against.

        Falls back to ADR-0040 §5's safest pair for a symbol the resolved map
        does not carry, which is the same answer the resolution itself gives an
        unconfigured traded symbol — so a read can never be more permissive than
        the map, whatever the caller asks for. A symbol outside the traded set
        has no position to value in the first place.
        """
        return self._leverage.get(symbol, LeverageSpec())

    def observe_mark(self, mark: MarkTick) -> None:
        """Take the latest mark for a symbol — the Tier-2 write verb (ADR-0039).

        Last-value-wins and nothing else: no history, no accumulation, and
        deliberately **no store write**, because a mark is an *input* the venue
        keeps sending rather than state this engine owns. Persisting it would
        make a restart recover a valuation instead of recomputing one, which is
        the whole distinction Tier-2 rests on.

        A late or superseded mark is therefore harmless, which is what licenses
        this half of the projection being bus-fed where Tier-1's is not.
        """
        self._marks[mark.symbol] = mark

    def apply_fill(self, event: OrderFillEvent, *, side: Side) -> LedgerChange:
        """Fold a fill into its partition and the cash line — the single write
        verb of the Tier-1 path.

        ``side`` comes from the saga rather than the event: ``OrderFillEvent``
        carries the trade and the ``Order`` carries the direction, and the
        ``ExecutionManager`` holds both. A redelivered fill is a no-op in both
        aggregates and returns a change with nothing in it, so it announces
        nothing.

        Returns rather than publishes: the caller must make the returned change
        durable before handing it back to ``project`` (ADR-0043 §4).
        """
        key = (event.strategy_id, event.symbol)
        position = self._positions.get(key)
        if position is None:
            # Deliberately not filed here — ``project`` files it, once the write
            # has landed. A fill the aggregate refuses therefore leaves no trace
            # in either place: a partition materialized by a refusal would
            # report a traded-flat record for a symbol that was never traded,
            # inverting ``position``'s ``None``. The refusal that reaches here is
            # the non-positive quantity — ``key`` is read off the event itself
            # and no other writer files a partition, so this seam cannot
            # misroute one.
            position = Position(strategy_id=event.strategy_id, symbol=event.symbol)
        realized_before = position.realized_pnl
        fees_before = position.fees
        changes = position.apply(event, side=side)
        if changes:
            # Realized PnL is one of the four accruing inputs to cash (ADR-0042
            # §4), and the position is what booked it — so the delta is read off
            # the aggregate rather than recomputed here. That read is also what
            # makes the cash line idempotent without a second applied set on
            # ``Account``: ``Position.apply`` is the fill's one gatekeeper, so a
            # redelivery books nothing and the delta it contributes here is zero
            # by construction. Guarding the call on ``changes`` is therefore
            # tidiness, not what keeps the line from double-counting.
            self._account.accrue_realized(
                position.realized_pnl - realized_before, event_id=event.event_id
            )
            # The fee is the second input, read off the aggregate for the same
            # reason the first is: the position booked it behind its own dedup,
            # so the delta a redelivery contributes is zero by construction and
            # the cash line needs no applied set to stay idempotent. Reading
            # ``event.fee`` here instead would bypass that gatekeeper and charge
            # a redelivered fill again. ``Account`` owns the sign (ADR-0042 §4).
            self._account.accrue_fee(position.fees - fees_before, event_id=event.event_id)
        return LedgerChange(account=self._account, position=position, changes=changes)

    def apply_funding(self, accrual: FundingAccrual) -> FundingChange | None:
        """Fold one settled boundary into the funding line and the cash line.

        The **gate comes first**, and it is a durable read rather than an
        in-memory one: an accrual whose boundary is at or below its symbol's
        watermark has already been applied and is dropped, returning ``None`` —
        nothing folded, nothing to write (ADR-0043 §5.2). That single value is
        what makes all three convergence paths no-ops across a restart, which an
        in-memory key set cannot be: paper's catch-up, live's reconcile
        re-ingest, and a replay rerun that re-derives every boundary in the file.

        The **gate is read before the split, never per partition**. A mark on the
        position row would be a value per row, and a row created *after* a
        boundary starts with no mark — so anything reaching back past its
        creation would find nothing to stop it, and an accrual dropped on one row
        but applied on another would still move ``cash``, leaving the ledger
        recording a payment one partition never sees.

        Cash moves by the amount **whether or not there is a partition to
        attribute it to**: the payment is a fact about the account, and the
        attribution is an accounting question with no bearing on whether it
        happened. That is ADR-0043 §5.2's decision rather than this method's
        convenience — the gate is read *before* the split precisely so the two
        questions are answered independently.

        **The two do come apart, and where they do the cash line is the one that
        stays right.** The venue computes the amount from the account net it read
        at the boundary; this folds it across the partitions held when it
        arrives. Let a fill take the symbol flat in between and ``exposed`` is
        empty: ``Σ funding lines`` falls short of the cash movement by the whole
        amount, and the mark advances in the same write, so no later pass
        corrects it. On the hermetic path that is unreachable — ``ReplayFeed``
        advances the clock and yields *before* it publishes, so the generator
        wakes with no cascade in flight and its own publish drains to quiescence
        before it returns. It is reachable wherever the generator instead wakes
        at an arbitrary point in the loop: a ``LiveClock`` paper run, where
        ``sleep_until`` is a real ``asyncio.sleep``, and ``KafkaBus``, where the
        accrual round-trips the broker and is dispatched behind whatever fills
        are already queued. The residue is the unattributed flow
        [#189](https://github.com/MarcosACH/tickwright/issues/189)'s reserved
        partition absorbs; until that partition exists the account line carries
        it alone, which is the direction that keeps ``cash`` faithful to what
        was actually paid.

        Returns rather than writes: the caller must make the change durable
        before the run goes on, exactly as ``apply_fill``'s does.
        """
        mark = self._store.funding_mark(accrual.symbol)
        if mark is not None and accrual.boundary_ts_ns <= mark:
            return None
        exposed = tuple(
            position
            for (_owner, symbol), position in self._positions.items()
            if symbol == accrual.symbol and not position.is_flat
        )
        self._split(accrual.amount, across=exposed)
        # ``Account`` owns no sign here, unlike the fee's leg: the amount already
        # mirrors the venue's ``usdc``, so a payment arrives negative (ADR-0037).
        self._account.accrue_funding(accrual.amount, event_id=accrual.event_id)
        return FundingChange(
            account=self._account,
            positions=exposed,
            funding_mark=(accrual.symbol, accrual.boundary_ts_ns),
        )

    @staticmethod
    def _split(amount: Decimal, *, across: tuple[Position, ...]) -> None:
        """Attribute one symbol's accrual across the partitions holding it.

        Pro-rata by signed size, which is not a policy choice but the identity
        that makes it exact: the amount is ``− net × price × rate`` and a
        partition's own share is ``− size_i × price × rate``, so scaling by
        ``size_i / net`` reproduces each partition's own accrual from the net
        one. A short partition against a longer one therefore *receives* out of a
        payment the account made, which is what actually happened.

        **The last partition takes the residue rather than its quotient**, so the
        shares sum to the amount exactly. ``Decimal`` division is inexact at any
        precision, and a rounded split would leave ``Σ funding lines`` short of
        the ``cash`` movement beside it — a discrepancy that accumulates every
        boundary and has no later step to correct it. The single-partition case,
        which is every paper run today, divides not at all.

        A net of zero is not divisible and needs no division. Offsetting
        partitions arise where the account is flat in the symbol, and a flat
        account accrues nothing to split — so on the hermetic path the guard is
        simply unreachable. Where it *is* reachable, the symbol having gone flat
        between the venue's read and this fold, the early return is the same
        decision ``apply_funding`` records one method up: the accrual stays whole
        on the cash line and attributes to nothing, rather than being forced onto
        a partition that no longer holds what it was charged for.
        """
        net = sum((position.signed_size for position in across), Decimal("0"))
        if not across or net == 0:
            return
        allocated = Decimal("0")
        for position in across[:-1]:
            share = amount * position.signed_size / net
            position.accrue_funding(share)
            allocated += share
        across[-1].accrue_funding(amount - allocated)

    def project_funding(self, change: FundingChange) -> None:
        """Announce the partitions one accrual moved — ``project``'s funding half.

        ADR-0045 §2 names ``position.changed`` for "a fill **or accrual** moves a
        non-flat record", and this is the accrual arm. It matters more here than
        on the fill path rather than less: funding is the one accounting input
        with no carrier event (ADR-0037), so unlike a fill there is no ``order.*``
        record beside it that an operator could read the movement off instead —
        and the catalog is already the only place a position change is observable
        at all, being an output derived from an input on the bus rather than a bus
        event of its own (ADR-0045 §1). A funding line that moved with no record
        moved invisibly.

        **Nothing is filed**, which is the difference from its sibling. An accrual
        splits only across partitions the projection already holds — it is read
        out of ``_positions`` to be split at all — so there is no first filing to
        defer behind the write, and the announcement is the whole of what this
        step keeps.

        One name and no branch: an accrual moves a record without ever opening or
        closing one, so ``_POSITION_EVENTS`` has nothing to choose between here
        and the map stays the fill path's. ``funding`` rides the record because it
        is what moved, beside the ``size`` that did not — a reader filtering on
        ``position.changed`` gets one shape from both emitters.
        """
        for position in change.positions:
            named_event(
                NamedEvent.POSITION_CHANGED,
                strategy_id=position.strategy_id,
                symbol=position.symbol,
                size=str(position.signed_size),
                funding=str(position.funding),
            )

    def project(self, change: LedgerChange) -> None:
        """File ``change``'s partition and announce what the fill did.

        The in-memory half of the atomic path, and the caller's promise that the
        change is already durable: a partition filed ahead of the write would be
        readable state the store never recorded, and a ``position.*`` record
        emitted ahead of it would name a change that a crash could still undo.

        The filing carries a partition's **first** fill and only that one: on
        every later fill the key already holds this very object, advanced in
        place by ``apply_fill``, so the announcement is the whole of what this
        step still keeps behind the write.
        """
        position = change.position
        self._positions[(position.strategy_id, position.symbol)] = position
        for change_kind in change.changes:
            named_event(
                _POSITION_EVENTS[change_kind],
                strategy_id=position.strategy_id,
                symbol=position.symbol,
                # The size *that change* produced, not the fill's end state. The
                # two differ only on a flip, where the old leg closes at zero
                # before the residual opens: reusing the post-fill size for both
                # halves would announce a close at the residual's size and
                # invert the name. This catalog is the only place a position
                # change is observable (ADR-0045 §1), so there is nothing else a
                # reader could reconcile that against.
                size="0" if change_kind is PositionChange.CLOSED else str(position.signed_size),
            )

    def recover(self) -> None:
        """Restore the ledger from the durable record, seeding paper's genesis
        row if the store holds none (ADR-0043 §6/§10).

        The runner's **first** recovery step, ahead of ``cache.rebuild()``:
        cumulative realized PnL, the fee and funding lines and per-strategy
        attribution are reconstructible from nowhere else (ADR-0043 §1), so a
        restart that skips this has silently lost them — and on paper there is no
        venue to heal them from.

        A store with no account row was never opened, so there is nothing to
        restore and the partitions are empty by the same fact; reading them would
        be a mass-read for rows that cannot exist.

        The store's disagreement refusals land **ahead of the seed**, on the one
        ``load_account`` this step already makes: a store this engine may not
        trade is refused before it is written to, and before the mass read behind
        this step ever runs.
        """
        stored = self._store.load_account()
        self._refuse_disagreement(stored)
        if stored is None:
            self._seed_genesis()
            return
        self._account = stored
        # The reserved unattributed partition comes back with everything else
        # (ADR-0043 §9): ADR-0034's Σ-invariant holds by construction only if it
        # is restored too, so the filtering belongs at the seam, not here.
        self._positions = {
            (position.strategy_id, position.symbol): position
            for position in self._store.all_positions()
        }

    def _refuse_disagreement(self, stored: Account | None) -> None:
        """Refuse a store this engine may not trade (ADR-0043 §10).

        Takes ``stored`` rather than reading it, so the field comparison is paid
        for by the one ``load_account`` ``recover`` already makes.

        **Two disjoint conditions**, and they cannot both fire: a missing account
        row leaves no fields to compare. The absent-row arm asks ``has_orders()``
        — an existence question, never the mass read — because this whole step
        runs *ahead* of ``cache.rebuild()`` precisely so a store that must not be
        traded is refused before its history is deserialized.
        """
        declared = self._spec.genesis_collateral
        if stored is None:
            # Paper-only, on ADR-0043 §10's one predicate: order history behind an
            # unopened ledger is the legitimate, self-healing state of a *live*
            # store that predates the ledger — the barrier materialises the row
            # and positions heal from venue truth. Paper has no venue to heal
            # from, so the same state is the §8 upgrade hazard.
            if declared is not None and self._store.has_orders():
                raise StoreAccountMismatch(
                    "this paper store holds order history but no ledger: its "
                    "positions, fees and funding cannot be reconstructed from "
                    "the orders alone (ADR-0043 §8), and seeding a fresh ledger "
                    "would report a flat account at full cash. Point the run at "
                    "a fresh store."
                )
            return
        disagreements = []
        if stored.account_id != self._spec.account_id:
            disagreements.append(
                ("account_id", repr(stored.account_id), repr(self._spec.account_id))
            )
        # Compared **only when the adapter declares one**: a ``None`` declaration
        # is no counterpart to check against, not a disagreement (ADR-0043 §10).
        # Ungated, live's stored genesis — ``equity − Σ unrealized_pnl``, ingested
        # at the barrier — would differ from its declared ``None`` on every start,
        # so a check meant to catch a swapped account would brick the path it was
        # never meant to police.
        if declared is not None and stored.genesis_collateral != declared:
            disagreements.append(
                ("genesis_collateral", str(stored.genesis_collateral), str(declared))
            )
        if disagreements:
            raise StoreAccountMismatch(_mismatch_report(disagreements))

    def _seed_genesis(self) -> None:
        """Open the durable ledger at the genesis the venue *declared* (ADR-0043 §6).

        Gated on ``genesis_collateral is not None`` — the predicate that says the
        opening balance was declared rather than ingested, which is the same fact
        as "this path has no venue to heal from" (ADR-0043 §10). On live the
        number is ``accountValue − Σ unrealized_pnl`` read at the startup barrier,
        which has not run yet, so seeding here would persist a genesis of zero as
        though someone had chosen it and leave the barrier correcting a row it was
        supposed to create.

        The in-memory account already opened at exactly this value, so the write
        makes the ledger durable rather than changing it.
        """
        if self._spec.genesis_collateral is None:
            return
        self._store.checkpoint_ledger(account=self._account, ts_ns=self._clock.timestamp_ns())

    def is_opened(self) -> bool:
        """Whether the **durable** ledger holds this account's row yet.

        The predicate the startup barrier's live-only materialisation reads
        (ADR-0043 §6, "solely to create the row when absent"), and it is
        deliberately about the row rather than about which venue this is: paper
        is skipped there because ``recover()`` seeded its row three steps
        earlier, and a live *restart* is skipped because its row was materialised
        in an earlier life. A venue-kind flag would say the same thing about the
        first case and the wrong thing about the second.

        ``False`` is therefore exactly one state — a live first start, before the
        barrier — and it is the only one in which a venue account read is owed.

        **Asked of the store rather than cached**, and that is not a detail: the
        row has a writer neither opener goes through — a fill's own
        ``checkpoint_ledger`` carries it, because ADR-0043 §9 makes ``account``
        required and every mutation moves cash. A flag maintained by the two
        openers would therefore answer "not open" against a row a fill had
        already written, and the barrier's whole ordering rationale rests on the
        opposite: a rebuild that ran first *creates* the row, and the
        materialisation behind it declines to overwrite rather than resetting the
        cash line that fill just moved. One question, one source — a store read
        cannot drift from the table it reads.

        A method rather than a property for the same reason: this reaches the
        store, and a property would invite a caller to treat it as a field. Both
        callers are startup-only, so the read is paid once per barrier attempt.
        """
        return self._store.load_account() is not None

    def materialise(self, state: VenueAccountState) -> None:
        """Open the durable ledger at the genesis ``state`` implies (ADR-0043 §6).

        Live's counterpart to ``_seed_genesis``, and the two differ only in where
        the number comes from: paper's is a config value already in hand, so it
        is written inside ``recover()``, while this one has to be read from the
        venue and so waits for the startup barrier, under the barrier's own
        bounded-retry-then-fault policy.

        **Refuses an already-open ledger**, which is where ADR-0042 §3's
        write-once rule belongs: this is the operation a second derivation would
        corrupt, and ADR-0047 §1 puts a precondition on the verb rather than on
        the caller that happens to observe it first. Nothing downstream could
        catch the overwrite — on live the genesis is *provenance only*, with no
        configured counterpart to compare against, so #188's refusal is
        paper-only by ADR-0043 §10's predicate. The barrier's caller checks
        ``is_opened`` too, and the two are not a doubled rule: the caller is
        deciding whether it owes the venue a *read*, this is deciding whether it
        may *write*. They cannot disagree — both ask the store the one question,
        and this one is the answer no future caller can inherit its way around.

        The order against the barrier's mass-rebuild is the caller's to keep and
        is load-bearing: the rebuild emits synthetic fills, every fill's write
        carries the account row (ADR-0043 §9), so a rebuild that ran first would
        *create* the row at the zero ``Account.open`` resolved — and this method,
        declining to overwrite it, would leave that zero standing for the life of
        the ledger with nothing to refuse it.
        """
        if self.is_opened():
            raise InvariantViolation(
                f"ledger for {self._spec.account_id} is already open: genesis is "
                "written once and never re-derived (ADR-0042 §3)"
            )
        # One clock read for both: the opening instant the row is stamped with
        # and the instant it was made durable are the same fact here, and two
        # reads would let a live clock put them a tick apart for no reason.
        ts_ns = self._clock.timestamp_ns()
        self._account = Account.ingest(self._spec, state, ts_ns=ts_ns)
        self._store.checkpoint_ledger(account=self._account, ts_ns=ts_ns)
        # Announced behind the write, as ``project`` is: a record naming an
        # opening balance a crash could still undo would be worse than none.
        named_event(
            NamedEvent.ACCOUNT_MATERIALISED,
            account_id=self._account.account_id,
            genesis_collateral=str(self._account.genesis_collateral),
        )

    def position(self, symbol: str, *, strategy_id: str | None) -> PositionView | None:
        """One partition's frozen snapshot, or ``None`` if never traded.

        **Recomputed on every call**, never memoized: the mark moves under the
        reader between two reads and a cached view would report the older one
        with no way to tell (ADR-0034).
        """
        position = self._positions.get((strategy_id, symbol))
        if position is None:
            return None
        return self._view(position, net=self._account_net())

    def open_positions(self, *, strategy_id: str | None) -> tuple[PositionView, ...]:
        """Every partition of ``strategy_id`` still holding exposure.

        One fold for the whole call, deliberately: the net is an aggregation over
        every partition, so folding it per view would be quadratic in the book —
        and, worse, would let two views in one returned tuple be computed
        against two different folds if a fill landed between them. The reads are
        synchronous so that cannot actually happen today; taking the fold once
        is what keeps it impossible rather than merely unreachable.
        """
        net = self._account_net()
        return tuple(
            self._view(position, net=net)
            for (owner, _symbol), position in self._positions.items()
            if owner == strategy_id and not position.is_flat
        )

    def _account_net(self) -> dict[str, Decimal]:
        """The account-net size per symbol, over *every* partition.

        The reserved unattributed one included: the venue holds one position per
        symbol, so the position-grain half is computed at that grain or it is
        computed against a book the venue does not have (ADR-0034/0041 §4).
        """
        return account_net_size(self._positions.values())

    def _view(self, position: Position, *, net: dict[str, Decimal]) -> PositionView:
        """Assemble one partition's view against the mark held for its symbol.

        The mark is resolved here, from the private cache, rather than passed in
        by the caller: pushing mark-sourcing onto every reader is exactly what
        the single always-available read path exists to prevent (ADR-0034/0039).
        """
        mark = self._marks.get(position.symbol)
        return position_view(
            position,
            account_net=net.get(position.symbol, _ZERO),
            mark=mark.price if mark is not None else None,
            mark_ts=mark.ts_event if mark is not None else None,
        )

    def account(self) -> AccountView:
        """The account-wide pool — one collateral bucket, never scoped.

        Every partition contributes, the reserved unattributed one included:
        flow the engine never placed still backs the same collateral, so leaving
        it out would report an equity the venue does not hold.
        """
        return account_view(
            self._account,
            positions=self._positions.values(),
            marks={symbol: mark.price for symbol, mark in self._marks.items()},
        )

    def for_strategy(self, strategy_id: str) -> Portfolio:
        """The scoped ``Portfolio`` facade the composition root injects into a
        strategy. Bound to a real ``strategy_id``, so the unattributed partition
        is structurally unreachable through it (ADR-0041 §5)."""
        return _ScopedPortfolio(projection=self, strategy_id=strategy_id)


@dataclass(frozen=True, slots=True)
class _ScopedPortfolio:
    """One strategy's view of the projection — the ``Portfolio`` seam's only impl.

    Holds no state of its own: it is the binding of a ``strategy_id`` to the
    projection, which is what lets the seam's methods take no scope argument.
    """

    projection: PortfolioProjection
    strategy_id: str

    def position(self, symbol: str) -> PositionView | None:
        return self.projection.position(symbol, strategy_id=self.strategy_id)

    def open_positions(self) -> tuple[PositionView, ...]:
        return self.projection.open_positions(strategy_id=self.strategy_id)

    def account(self) -> AccountView:
        return self.projection.account()
