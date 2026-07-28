"""The single-account aggregate and the venue's static declaration about it.

One ``Engine`` process trades exactly one account (ADR-0038), so ``Account`` is
a deployment fact rather than a collection: an opening declaration written once
and a Tier-1 ``cash`` line that accumulates away from it.

**The cash line has exactly four accruing inputs** (ADR-0042 §4) — genesis,
``+`` realized PnL, ``−`` fees, ``+`` funding — and nothing else may move it by
accrual. The set is closed on purpose: it is what makes the equity identity
checkable and gives the durable ledger a finite list of writers to enforce. Each
input is its own named method, so "what may move cash" is answerable by reading
this class. (Live reconciliation may additionally *correct* the line toward
venue truth; that is a correction, not a fifth input.)
"""

from dataclasses import dataclass
from decimal import Decimal

from .enums import Netting


@dataclass(frozen=True, slots=True, kw_only=True)
class AccountSpec:
    """The venue adapter's static declarations about the account it trades.

    ``AccountSpec`` is to ``Account`` as ``InstrumentSpec`` is to the
    instrument: declaration, never live balances (ADR-0038). ``account_id`` is
    qualified — ``paper-<label>`` on the deterministic venue, venue + network +
    venue-native identifier on a live one. ``genesis_collateral`` is the
    operator's declared opening cash on paper and ``None`` on live, where the
    number is ingested from the venue instead (ADR-0042 §6); that nullability
    is the predicate the startup checks read, not an optional setting.
    """

    account_id: str
    netting: Netting = Netting.NET
    genesis_collateral: Decimal | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class AccountView:
    """The frozen account-wide pool snapshot the ``Portfolio`` seam returns.

    Carries no realized PnL and no liquidation price — both are wrong at this
    grain (ADR-0041 §2/§4). ``cash`` is Tier-1 and therefore never ``None``, so
    a cold start is not a reporting blackout (ADR-0041 §6).
    """

    cash: Decimal


class Account:
    """One collateral pool's Tier-1 cash line, plus the declaration it opened on."""

    __slots__ = ("_account_id", "_applied_event_ids", "_cash", "_genesis", "_genesis_ts_ns")

    def __init__(self, *, account_id: str, genesis_collateral: Decimal, genesis_ts_ns: int) -> None:
        self._account_id = account_id
        self._genesis = genesis_collateral
        self._genesis_ts_ns = genesis_ts_ns
        # The cash line starts *at* genesis and accrues away from it; the
        # declaration above is retained separately as the account's identity,
        # never recomputed from the line (ADR-0042 §3).
        self._cash = genesis_collateral
        self._applied_event_ids: set[str] = set()

    @property
    def account_id(self) -> str:
        """The venue-qualified identity this ledger belongs to (ADR-0038)."""
        return self._account_id

    @property
    def genesis_collateral(self) -> Decimal:
        """The opening cash this account was created at — written once."""
        return self._genesis

    @property
    def genesis_ts_ns(self) -> int:
        """When the account was opened, UTC epoch ns — written once."""
        return self._genesis_ts_ns

    @property
    def cash(self) -> Decimal:
        """The Tier-1 collateral balance. Moves only through this class's
        accrual methods, which are the closed write-set (ADR-0042 §4)."""
        return self._cash

    @property
    def applied_event_ids(self) -> frozenset[str]:
        """The reflected ``event_id``s — the dedup set a ``Store`` round-trips."""
        return frozenset(self._applied_event_ids)

    def view(self) -> AccountView:
        """A frozen Tier-1 snapshot of the account-wide pool."""
        return AccountView(cash=self._cash)

    def accrue_realized(self, amount: Decimal, *, event_id: str) -> bool:
        """``+`` realized PnL, one of the four accruing inputs (ADR-0042 §4).

        Signed: a loss accrues negative. Idempotent on ``event_id`` on the same
        terms as ``Position.apply``, so a redelivered fill moves no cash;
        returns whether this call moved the line.
        """
        if event_id in self._applied_event_ids:
            return False
        self._applied_event_ids.add(event_id)
        self._cash += amount
        return True

    @classmethod
    def open(cls, spec: AccountSpec, *, genesis_collateral: Decimal, ts_ns: int) -> "Account":
        """Seed a fresh ledger for ``spec`` at ``genesis_collateral``.

        The collateral is passed rather than read off the spec because live
        declares ``None`` there and ingests its opening value from the venue —
        resolving the two is the composition root's job (ADR-0042 §6).
        """
        return cls(
            account_id=spec.account_id,
            genesis_collateral=genesis_collateral,
            genesis_ts_ns=ts_ns,
        )

    def __repr__(self) -> str:
        return (
            f"Account(account_id={self._account_id!r}, "
            f"genesis_collateral={self._genesis}, cash={self._cash})"
        )
