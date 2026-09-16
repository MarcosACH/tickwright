"""The real store, recording the recovery reads whose order and count are the
contract.

Two suites assert the boot's read discipline: ``tests/engine/test_checkpoint.py``
owns the rule (the ledger's ``load_account`` runs ahead of the ``Cache``'s
``all_orders``, ADR-0043 §6/§10) and ``tests/engine/test_runner_e2e.py`` pins
that the runner recovers through it, and that the mass read happens once per
boot (issue #233). Neither read leaves a distinguishing durable trace, so the
seam they share is the one observation port. Recorded, not simulated: every
call still reaches the real store underneath.
"""

from pathlib import Path

from tickwright.adapters.store import SQLiteStore
from tickwright.domain import Account, Order


class RecoveryOrderStore(SQLiteStore):
    """A ``SQLiteStore`` that appends each recovery read to ``timeline``."""

    def __init__(self, timeline: list[str], path: str | Path = ":memory:") -> None:
        super().__init__(path)
        self._timeline = timeline

    def load_account(self) -> Account | None:
        self._timeline.append("ledger.load_account")
        return super().load_account()

    def all_orders(self) -> list[Order]:
        self._timeline.append("cache.all_orders")
        return super().all_orders()
