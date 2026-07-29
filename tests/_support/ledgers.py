"""Ready-made accounting wiring for suites whose subject is not the accounting
surface.

The ``ExecutionManager`` requires a ``Checkpointer`` — it is the read-models'
single writer, and making that optional would ship a manager whose accounting
can be silently absent. Most suites care about the saga rather than the ledger,
so they take one from ``checkpointer`` below rather than open a paper account by
hand at every wiring site. An ``Engine`` needs none: it opens its own over the
store it is given (#213), and hands out the scoped facade through
``portfolio_for``.

Suites that *are* about the accounting surface (``tests/engine/test_portfolio.py``,
``tests/engine/test_tracer_e2e.py``) build theirs explicitly, because how the
ledger is opened is part of what they assert.

The ``GENESIS`` below is the other half of what those saga-focused suites take from
here: the one opening balance they wire their *venue* with too, so no suite declares
the number twice.
"""

from decimal import Decimal

from tickwright.adapters.clock import ManualClock
from tickwright.domain import AccountSpec, Clock, OrderFillEvent, Side, Store
from tickwright.engine.checkpoint import Checkpointer
from tickwright.engine.portfolio import PortfolioProjection

GENESIS = Decimal("100000")
"""The opening cash a saga-focused suite opens both its ledger and its venue with.

The value itself is arbitrary and positive, the one thing such a suite needs of it
(ADR-0042 §1 forbids a *venue* default, not a test's declaration). What matters is
that the ledger seeded here and the ``genesis_collateral`` handed to the
``PaperExchange`` beside it read the same number: #188 raises
``StoreAccountMismatch`` on the genesis condition before any other recovery work,
so a suite that opens the two from separate declarations is a suite that can fail
for a reason unrelated to what it asserts."""


def _spec(genesis: Decimal) -> AccountSpec:
    return AccountSpec(account_id="paper-default", genesis_collateral=genesis)


def checkpointer(store: Store, *, clock: Clock, genesis: Decimal = GENESIS) -> Checkpointer:
    """The manager's one collaborator, over the suite's own ``store`` and clock.

    This is what a saga-focused suite wires: the ``Checkpointer`` builds both
    read-models from the single ``store`` handed in, so the fill's one
    transaction (ADR-0043 §4) is one by construction and a suite can no longer
    split it by pointing an order cache and a ledger at two stores. Reach the
    projections through ``.cache`` / ``.portfolio`` where a case reads them back.

    ``clock`` is required for the same reason ``store`` is, and it is the reason
    neither is defaulted here: the manager takes its time source from the
    ``Checkpointer``, so a private default would let a suite advance its venue's
    clock while the saga's stamps stayed frozen on another — the two-timeline
    split the type exists to make unwireable, reintroduced by the helper that
    wires it.

    Override ``genesis`` only where the opening balance is itself the subject; a
    suite that also wires a venue must move both or neither.
    """
    return Checkpointer(spec=_spec(genesis), store=store, clock=clock)


def ledger(store: Store, *, genesis: Decimal = GENESIS) -> PortfolioProjection:
    """A bare ledger, for the few cases that read the accounting surface without
    a saga in sight — a strategy harness, say, whose subject is the ``Portfolio``
    seam rather than the write path that moves it.

    Anything wiring an ``ExecutionManager`` wants ``checkpointer`` above instead:
    the manager no longer takes a projection on its own, precisely so that the
    store behind it cannot diverge from the order cache's.
    """
    return PortfolioProjection(spec=_spec(genesis), store=store, clock=ManualClock(start_ns=0))


def book_fill(projection: PortfolioProjection, event: OrderFillEvent, *, side: Side) -> None:
    """Fold ``event`` into ``projection`` and project it — the atomic path's two
    steps, minus the durable write that belongs between them (ADR-0043 §4).

    The ``ExecutionManager`` is the real caller and the only one holding a store,
    so that write is asserted at *its* seam. A suite whose subject is what a fill
    makes readable stands in for it here rather than repeating the pair.
    """
    projection.project(projection.apply_fill(event, side=side))
