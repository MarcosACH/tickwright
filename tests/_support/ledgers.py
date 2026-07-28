"""A ready-made ledger for suites whose subject is not the accounting surface.

The ``ExecutionManager`` and the ``Engine`` both require a ``PortfolioProjection``
— the manager is the projection's single writer, and making that optional would
ship an engine whose accounting can be silently absent. Most suites care about
the saga rather than the ledger, so they take one from here rather than
open a paper account by hand at every wiring site.

Suites that *are* about the accounting surface (``tests/engine/test_portfolio.py``,
``tests/engine/test_tracer_e2e.py``) build theirs explicitly, because how the
ledger is opened is part of what they assert.

The ``GENESIS`` below is the other half of that: the one opening balance those
suites also wire their *venue* with, so no suite declares the number twice.
"""

from decimal import Decimal

from tickwright.domain import Account, AccountSpec
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


def ledger(genesis: Decimal = GENESIS) -> PortfolioProjection:
    """A fresh in-memory ledger on a paper-shaped account.

    Override ``genesis`` only where the opening balance is itself the subject; a
    suite that also wires a venue must move both or neither.
    """
    spec = AccountSpec(account_id="paper-default", genesis_collateral=genesis)
    return PortfolioProjection(account=Account.open(spec, ts_ns=0))
