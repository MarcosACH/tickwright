"""A ready-made ledger for suites whose subject is not the accounting surface.

The ``ExecutionManager`` requires a ``PortfolioProjection`` — it is the
projection's single writer, and making that optional would ship a manager whose
accounting can be silently absent. Most suites care about the saga rather than
the ledger, so they take one from here rather than open a paper account by hand
at every wiring site. An ``Engine`` needs none: it opens its own over the store
it is given (#213), and hands out the scoped facade through ``portfolio_for``.

Suites that *are* about the accounting surface (``tests/engine/test_portfolio.py``,
``tests/engine/test_tracer_e2e.py``) build theirs explicitly, because how the
ledger is opened is part of what they assert.

The ``GENESIS`` below is the other half of what those saga-focused suites take from
here: the one opening balance they wire their *venue* with too, so no suite declares
the number twice.
"""

from decimal import Decimal

from tickwright.adapters.clock import ManualClock
from tickwright.domain import AccountSpec, OrderFillEvent, Side, Store
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


def ledger(store: Store, *, genesis: Decimal = GENESIS) -> PortfolioProjection:
    """A fresh ledger on a paper-shaped account, over the suite's own ``store``.

    The ``store`` is required and has no default: a fill writes the order row and
    the ledger rows in one transaction (ADR-0043 §4), so a projection pointed at
    a *different* store than the ``Cache`` splits the one write the atomicity
    argument rests on — and a suite that gets that wrong produces the split with
    no failing assertion. The parameter used to be optional and the rule lived
    in this docstring as a request; an ``Engine`` now opens its own ledger over
    its own store (#213), so the sites that took the default have none to take.

    Override ``genesis`` only where the opening balance is itself the subject; a
    suite that also wires a venue must move both or neither.
    """
    return PortfolioProjection(
        spec=AccountSpec(account_id="paper-default", genesis_collateral=genesis),
        store=store,
        clock=ManualClock(start_ns=0),
    )


def book_fill(projection: PortfolioProjection, event: OrderFillEvent, *, side: Side) -> None:
    """Fold ``event`` into ``projection`` and project it — the atomic path's two
    steps, minus the durable write that belongs between them (ADR-0043 §4).

    The ``ExecutionManager`` is the real caller and the only one holding a store,
    so that write is asserted at *its* seam. A suite whose subject is what a fill
    makes readable stands in for it here rather than repeating the pair.
    """
    projection.project(projection.apply_fill(event, side=side))
