"""A ready-made ledger for suites whose subject is not the accounting surface.

The ``ExecutionManager`` and the ``Engine`` both require a ``PortfolioProjection``
— the manager is the projection's single writer, and making that optional would
ship an engine whose accounting can be silently absent. Most suites care about
the saga rather than the ledger, so they take one from here rather than
open a paper account by hand at every wiring site.

Suites that *are* about the accounting surface (``tests/engine/test_portfolio.py``,
``tests/engine/test_tracer_e2e.py``) build theirs explicitly, because how the
ledger is opened is part of what they assert.
"""

from decimal import Decimal

from tickwright.domain import Account, AccountSpec
from tickwright.engine.portfolio import PortfolioProjection

GENESIS = Decimal("100000")
"""The opening cash these ledgers are seeded with — arbitrary and positive, the
one thing a saga-focused test needs of it (ADR-0042 §1 forbids a *venue* default,
not a test's declaration)."""


def ledger(genesis: Decimal = GENESIS) -> PortfolioProjection:
    """A fresh in-memory ledger on a paper-shaped account."""
    spec = AccountSpec(account_id="paper-default", genesis_collateral=genesis)
    return PortfolioProjection(account=Account.open(spec, ts_ns=0))
