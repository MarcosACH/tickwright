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
from .events import VenueAccountState


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

    __slots__ = ("_account_id", "_cash", "_genesis", "_genesis_ts_ns")

    def __init__(self, *, account_id: str, genesis_collateral: Decimal, genesis_ts_ns: int) -> None:
        self._account_id = account_id
        self._genesis = genesis_collateral
        self._genesis_ts_ns = genesis_ts_ns
        # The cash line starts *at* genesis and accrues away from it; the
        # declaration above is retained separately as the account's identity,
        # never recomputed from the line (ADR-0042 §3).
        self._cash = genesis_collateral

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

    def view(self) -> AccountView:
        """A frozen Tier-1 snapshot of the account-wide pool."""
        return AccountView(cash=self._cash)

    def accrue_realized(self, amount: Decimal, *, event_id: str) -> None:
        """``+`` realized PnL, one of the four accruing inputs (ADR-0042 §4).

        Signed: a loss accrues negative.

        **Unconditional: this class writes the cash line, it does not police
        what reaches it.** Each accruing input arrives already deduplicated by
        the gatekeeper that owns its key — fills by ``Position.apply`` on
        ``event_id``, funding by the per-symbol watermark at ``(symbol,
        boundary_ts)`` grain (ADR-0037, ADR-0043 §5.2) — so an applied-event set
        here would guard nothing and shadow those keys. It would also give one
        fill's realized and fee legs a single dedup entry, silently dropping the
        second: they ride one ``OrderFillEvent`` and so share one ``event_id``.
        ADR-0043 §4 rejects a ledger-side applied set on the durable row for the
        same reason it does not belong in memory — it grows for the life of the
        ledger, where a saga's set dies with its order.

        ``event_id`` is therefore provenance, not a key: it says *which fact*
        moved the line, which is what ADR-0043 §1's deferred line-item audit log
        records if it is ever taken up.
        """
        self._cash += amount

    @classmethod
    def restore(
        cls,
        *,
        account_id: str,
        genesis_collateral: Decimal,
        genesis_ts_ns: int,
        cash: Decimal,
    ) -> "Account":
        """Rebuild a persisted account, cash line and all (ADR-0043 §9).

        The sibling of ``Order.restore``: ``open`` seeds a *fresh* ledger, where
        cash is genesis by definition, so it cannot express an account that has
        since accrued. Recovery restores the two independently because the store
        holds them as two columns — the opening declaration that never moves and
        the line that has.
        """
        account = cls(
            account_id=account_id,
            genesis_collateral=genesis_collateral,
            genesis_ts_ns=genesis_ts_ns,
        )
        account._cash = cash
        return account

    @classmethod
    def open(cls, spec: AccountSpec, *, ts_ns: int) -> "Account":
        """Seed a fresh ledger at the opening cash ``spec`` declares.

        Paper declares the operator's number, which ADR-0042 §1 demands and
        never defaults, so a paper ledger opens at exactly what was configured.
        Live declares ``None``: its opening state is ingested from the venue as
        ``accountValue − Σ unrealized_pnl`` at the startup barrier (ADR-0042 §6),
        which has not run at the moment a ledger is opened — so a fresh live
        ledger opens at zero and the barrier materialises the real account row
        (ADR-0043 §6).

        That zero is what an *unstarted* live ledger reports, not a collateral
        default standing in for a number nobody chose: §1 rejects the latter,
        and only for paper, where the config validator makes ``None``
        unreachable. Tier-1 forbids reporting ``None`` here (ADR-0041 §6), so
        the unstarted state has to be *some* number. The ``None`` itself stays
        on the spec as the predicate the startup checks read (ADR-0042 §6);
        what this resolves is only what the ledger reads until they run.
        """
        genesis = spec.genesis_collateral
        return cls(
            account_id=spec.account_id,
            genesis_collateral=genesis if genesis is not None else Decimal("0"),
            genesis_ts_ns=ts_ns,
        )

    @classmethod
    def ingest(cls, spec: AccountSpec, state: VenueAccountState, *, ts_ns: int) -> "Account":
        """Open a fresh ledger at the genesis the *venue* reports (ADR-0042 §6).

        The third way an account comes into being, and the live path's: ``open``
        takes the operator's declaration, ``restore`` takes the durable row, and
        this takes the venue's own account read — which is why live configures no
        collateral at all. Configuring one would invent a number the venue
        already knows, contradict ADR-0034's venue-is-authoritative rule for
        Tier-1, and fail-fast on every legitimate deposit.

        ``genesis = equity − Σ unrealized_pnl``, and **the subtraction is
        load-bearing rather than tidy**: the venue's equity figure already
        contains unrealized PnL, so writing it straight into the cash line would
        double-count that PnL the instant ``equity = cash + Σ uPnL``
        (ADR-0040 §7) was evaluated — an account opened holding a position would
        report its uPnL twice from the very first cycle.

        Driven once, at the startup barrier, and only when the store holds no
        account row (ADR-0043 §6): the value is **provenance only** on this path
        — nothing cross-checks it, because there is no configured counterpart to
        check against — so re-deriving it on a later start would silently move a
        line that is supposed to have been written once.
        """
        genesis = state.equity - sum(
            (position.unrealized_pnl for position in state.positions), Decimal("0")
        )
        return cls(
            account_id=spec.account_id,
            genesis_collateral=genesis,
            genesis_ts_ns=ts_ns,
        )

    def __repr__(self) -> str:
        return (
            f"Account(account_id={self._account_id!r}, "
            f"genesis_collateral={self._genesis}, cash={self._cash})"
        )
