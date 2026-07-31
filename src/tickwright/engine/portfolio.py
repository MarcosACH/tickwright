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

from dataclasses import dataclass

from tickwright.domain import (
    Account,
    AccountSpec,
    AccountView,
    Clock,
    OrderFillEvent,
    Portfolio,
    Position,
    PositionChange,
    PositionView,
    Side,
    Store,
    VenueAccountState,
)
from tickwright.observability import NamedEvent, named_event

# What a fill did to a position → its cataloged name (ADR-0045 §2). A position
# change is never a bus event — it is an output derived from a fill already on
# the bus — so the named-event catalog is the only place it is observable from
# outside, and a ``PositionChange`` missing from this map fails with a KeyError.
_POSITION_EVENTS: dict[PositionChange, NamedEvent] = {
    PositionChange.OPENED: NamedEvent.POSITION_OPENED,
    PositionChange.CHANGED: NamedEvent.POSITION_CHANGED,
    PositionChange.CLOSED: NamedEvent.POSITION_CLOSED,
}


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


class PortfolioProjection:
    """The one owner of "what do I hold, and what has it earned"."""

    def __init__(self, *, spec: AccountSpec, store: Store, clock: Clock) -> None:
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
        return LedgerChange(account=self._account, position=position, changes=changes)

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

        (The store's disagreement refusals — a swapped account, a paper store
        with order history and no ledger — are #188's, and land ahead of the seed
        in this same step.)
        """
        stored = self._store.load_account()
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

        Driven only when ``is_opened()`` is ``False``, which is what keeps a live
        restart from re-deriving a genesis that ADR-0042 §3 wrote once.

        The order against the barrier's mass-rebuild is the caller's to keep and
        is load-bearing: the rebuild emits synthetic fills, every fill's write
        carries the account row (ADR-0043 §9), so a rebuild that ran first would
        *create* the row at the zero ``Account.open`` resolved — and this method,
        declining to overwrite it, would leave that zero standing for the life of
        the ledger with nothing to refuse it.
        """
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
        """One partition's frozen Tier-1 snapshot, or ``None`` if never traded."""
        position = self._positions.get((strategy_id, symbol))
        return position.view() if position is not None else None

    def open_positions(self, *, strategy_id: str | None) -> tuple[PositionView, ...]:
        """Every partition of ``strategy_id`` still holding exposure."""
        return tuple(
            position.view()
            for (owner, _symbol), position in self._positions.items()
            if owner == strategy_id and not position.is_flat
        )

    def account(self) -> AccountView:
        """The account-wide pool — one collateral bucket, never scoped."""
        return self._account.view()

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
