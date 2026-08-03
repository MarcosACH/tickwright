"""``Checkpointer`` — the ``Store``'s two read-models and the ordered writes
that keep all three in step.

The order ``Cache`` (ADR-0009) and the ``PortfolioProjection`` (ADR-0035) are
projections of *one* ``Store``, and a fill's write spans both in a single
transaction (ADR-0043 §4). That identity — ``store is cache._store is
portfolio._store`` — is what the atomicity argument rests on, and no type
expressed it: the three arrived at the ``ExecutionManager`` as three parameters
that a caller had to remember to point at one store. This constructs both from
the one ``Store`` it is given, so an engine whose two read-models write
different stores is not constructible.

Enforcing it here rather than with a runtime check is ADR-0047's rule read the
way its closing consequence permits — *"type-level enforcement is not
foreclosed"* — and the store identity is a wiring **choice**, so it belongs where
it is declared rather than on a write verb that could only discover it too late.

The run's ``Clock`` is held for the same reason and on the same argument: the
stamps on a saga's events and on its durable writes must come from one timeline,
and two parameters could be pointed at two.

The write verbs are the other half. Each ordering this owns is a rule a caller
would otherwise have to know and could silently invert:

- **the fill write** — fold, then one transaction, then project both read-models
  (``checkpoint_fill``)
- **the funding write** — gate on the durable mark, then one transaction that
  carries the mark's advance beside the line it guards (``checkpoint_funding``)
- **the non-fill write** — store first, then project (``checkpoint``)
- **recovery** — the ledger before the order cache (``recover``, ADR-0043 §6/§10)

Reads stay on the projections themselves: ``cache`` and ``portfolio`` are lent
out for the ``Reconciler``'s worklist, the manager's saga lookups and the scoped
facade a strategy holds. This type owns what *moves* them, not what asks them.
"""

from tickwright.domain import (
    AccountSpec,
    Clock,
    FundingAccrual,
    InvariantViolation,
    Order,
    OrderFillEvent,
    Side,
    Store,
)

from .cache import Cache
from .portfolio import PortfolioProjection


class Checkpointer:
    """The one writer of the order ``Cache`` and the ``PortfolioProjection``."""

    def __init__(self, *, spec: AccountSpec, store: Store, clock: Clock) -> None:
        self._store = store
        self._clock = clock
        # Both projections, from the one store above — the identity the fill's
        # single transaction rests on, made structural by being unable to take a
        # second store at all. The account the ledger opens against is the
        # venue's own declaration, and the spec goes over rather than an
        # ``Account`` opened from it: recovery reads ``genesis_collateral is not
        # None`` to tell a *declared* opening balance from an *ingested* one
        # (ADR-0043 §10), and an ``Account`` has resolved that away.
        self._cache = Cache(store=store)
        self._portfolio = PortfolioProjection(spec=spec, store=store, clock=clock)

    @property
    def clock(self) -> Clock:
        """The run's one timeline, lent to the ``ExecutionManager`` beside the
        read-models.

        Held here for the same reason as the ``Store``: a saga's event stamps
        (``ts_init``) and the durable stamps of the writes below must come from
        one clock, or a store's order history and the events announcing it read
        two timelines. Two ``Clock`` parameters could be pointed at two, exactly
        as two ``Store`` parameters could — so there is one, and it arrives with
        the projections it timestamps.
        """
        return self._clock

    @property
    def cache(self) -> Cache:
        """The order read-model, lent for reads (the reconciler's worklist, the
        manager's saga lookups). Writes go through this type's verbs."""
        return self._cache

    @property
    def portfolio(self) -> PortfolioProjection:
        """The accounting read-model, lent for reads, for the scoped
        ``Portfolio`` facade the composition root injects into a strategy, and —
        the one borrow that *writes* — for the startup barrier's account
        materialisation (``runner.py``). That write stays off this type's verbs
        deliberately: what these own is an ordering a caller could silently
        invert, and opening a ledger is one write to one read-model with no
        ordering inside it. The ordering it does have is the barrier's, which is
        the runner's to keep."""
        return self._portfolio

    def recover(self) -> None:
        """Restore both read-models from the store — the ledger first.

        The order is load-bearing rather than tidy (ADR-0043 §6/§10): the ledger
        asks for the account row and the partitions behind it, where the rebuild
        deserializes every saga in the store — partitions are bounded by strategy
        × symbol, sagas by all the history the store holds. Behind the rebuild, a
        restart that must not trade at all would pay that mass read before
        finding out; the refusal that makes such a restart possible is #188's,
        and lands ahead of the seed in this same step.

        It is also where paper's genesis row is written, so ``account()`` has a
        cash line long before a strategy is ever let near one.
        """
        self._portfolio.recover()
        self._cache.rebuild()

    def checkpoint(self, order: Order) -> None:
        """Make a **non-fill** transition durable, then project it.

        Store first — the projection must never be ahead of the durable record,
        or a crash between the two would recover less than readers already saw.
        None of these transitions touches the ledger, so the narrow
        ``Store.checkpoint`` is the right write for them (ADR-0043 §4).

        A checkpoint the store cannot make durable is a broken engine assumption
        (ADR-0014): fail fast rather than run a saga whose memory and durable
        states silently diverge.
        """
        try:
            self._cache.checkpoint(order, ts_ns=self._clock.timestamp_ns())
        except InvariantViolation as exc:
            # Only the seam's own failure type is relabelled (the same rule
            # ``checkpoint_fill`` states in full below) — and this wrapper spans
            # ``Cache.checkpoint``, which writes the store and *then* projects,
            # so a broader catch could report a failed write for a row already
            # durable.
            raise InvariantViolation(
                f"checkpoint write failed for cloid {order.cloid} in state {order.state.value}"
            ) from exc

    def checkpoint_fill(self, order: Order, event: OrderFillEvent, *, side: Side) -> None:
        """Make a fill durable across both read-models in one transaction.

        The fill is the only transition that mutates two read-models, and as two
        writes either ordering loses (ADR-0043 §4): order row first drops the
        fill from the ledger on a crash between them, ledger first double-counts
        it. On paper neither ever heals — the in-process venue holds no position
        state, so this store is the ledger's sole authority.

        The three steps cannot collapse into one call, which is why the ordering
        is a rule and not an implementation detail: the write takes the *folded*
        state as its input, so the fold cannot follow the write, and the
        projections must not precede it or a reader could reach state the store
        never recorded. ``side`` rides the saga because the event carries the
        trade and the order carries the direction.

        A refused write therefore leaves both aggregates ahead of the store —
        ``Order.record_fill`` has already advanced the saga the ``Cache`` holds
        by reference, and the fold has already moved the position and the cash
        line. What makes that safe is the raise rather than the ordering: an
        ``InvariantViolation`` faults the run (ADR-0014), so nothing goes on to
        trade or report against a read-model the store never recorded.
        """
        change = self._portfolio.apply_fill(event, side=side)
        ts_ns = self._clock.timestamp_ns()
        try:
            self._store.checkpoint_ledger(
                order=order,
                positions=(change.position,),
                account=change.account,
                ts_ns=ts_ns,
            )
        except InvariantViolation as exc:
            # Only the seam's own failure type is relabelled. ``InvariantViolation``
            # is the whole of the ``Store``'s error contract (ADR-0019), so anything
            # else crossing it is a bug *below* the seam, not a failed write — and
            # "the ledger did not move" is exactly the diagnosis a store broken
            # that way does not license.
            raise InvariantViolation(
                f"ledger checkpoint write failed for cloid {order.cloid} "
                f"in state {order.state.value}"
            ) from exc
        self._cache.project(order, ts_ns=ts_ns)
        self._portfolio.project(change)

    def checkpoint_funding(self, accrual: FundingAccrual) -> None:
        """Make one settled funding boundary durable — gate, then one transaction.

        The ordering a caller could invert is the same one ``checkpoint_fill``
        keeps, minus the order row and plus the watermark: the projection folds
        the accrual across its partitions and the cash line, and the write that
        makes that durable is the write that advances the mark. **The only write
        that may advance the mark is the one that applies the accrual**
        (ADR-0043 §9) — split them and the mark falls behind the sum it guards,
        which is §5.2's double-count reached from the other side.

        A **dropped** accrual writes nothing at all. The gate is the projection's
        (it holds the store read and the partitions), so a ``None`` here means
        the boundary was already applied and there is no state to persist —
        re-stamping the row would make a re-delivery indistinguishable from a
        payment by anything but the number.

        There **is** something to project behind the write, and it is narrower
        than the fill path's: no partition to file — an accrual only ever splits
        across partitions the projection already holds — but one
        ``position.changed`` per partition it moved, which is what ADR-0045 §2
        names for "a fill or accrual moves a non-flat record". Behind the write
        for the fill path's reason: a record naming a payment a crash could still
        undo would report money that never left.

        **Synchronous, like its siblings**, even though its one production caller
        is a bus subscriber and subscribers are ``async``. The adapting belongs
        to the runner that does the subscribing, not here: what makes the fold
        and the write safe is that no yield point separates them, and a
        coroutine on this verb would put the seam that guarantees it inside the
        type rather than around it.
        """
        change = self._portfolio.apply_funding(accrual)
        if change is None:
            return
        try:
            self._store.checkpoint_ledger(
                account=change.account,
                positions=change.positions,
                funding_mark=change.funding_mark,
                ts_ns=self._clock.timestamp_ns(),
            )
        except InvariantViolation as exc:
            # Only the seam's own failure type is relabelled, as above: anything
            # else crossing it is a bug below the store, not a failed write.
            raise InvariantViolation(
                f"funding checkpoint write failed for {accrual.symbol} "
                f"at boundary {accrual.boundary_ts_ns}"
            ) from exc
        self._portfolio.project_funding(change)
