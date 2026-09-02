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

_ZERO = Decimal("0")


def venue_cash(state: VenueAccountState) -> Decimal:
    """The cash line a venue snapshot implies — ``equity − Σ unrealized_pnl``.

    The venue reports no cash line of its own, so this is how one is recovered:
    ADR-0040 §7's ``equity = cash + Σ uPnL`` read backwards. **The subtraction
    is load-bearing rather than tidy** — the venue's equity already contains
    open PnL, so anything comparing or copying it into a cash line without
    removing that term is off by the whole of it on every account holding a
    position.

    One definition because it has two callers whose disagreement would be
    invisible: ``Account.ingest`` writes it as the genesis at the startup
    barrier, and the live reconcile cycle compares the accumulated cash line
    against it every pass (ADR-0034). Derived two ways, a drift between them
    would read as a permanent Tier-1 divergence on a ledger that opened
    perfectly correctly, and the heal would chase a figure no venue holds.

    It lives here rather than in ``valuation.py``, whose forward direction of
    the same identity it inverts, because ``valuation`` imports this module for
    ``Account`` and the reverse edge would close a cycle. This is also the
    module the quantity belongs to on its own terms: what it names is a cash
    line, which is the one thing ``Account`` accumulates.
    """
    return state.equity - sum((position.unrealized_pnl for position in state.positions), _ZERO)


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

    Assembled by ``domain.valuation``, never by the aggregate: every Tier-2 Σ
    here ranges over positions the ``Account`` knows nothing about.

    **No field defaults**, for the reason ``PositionView`` states: a defaulted
    ``equity`` would let a view be built claiming the Σ was uncomputable without
    any term having been examined.
    """

    cash: Decimal
    equity: Decimal | None
    """``cash + Σ unrealized_pnl`` over **every** position — the account's real
    backing value (ADR-0041 §4).

    ``None`` iff a contributing term needs a mark that is absent, which is a
    genuine cross-strategy coupling: one un-marked symbol makes the total
    uncomputable for every strategy, because equity is an account-wide fact and
    a partial sum of it is a different quantity wearing its name (ADR-0041 §6).
    An account holding nothing has no such term and reads its cash line."""
    total_margin_used: Decimal | None
    """Σ ``margin_used`` over every position the account holds (ADR-0041 §4).

    Over the **venue's** positions, one per symbol, never over our partitions of
    them: ``margin_used`` is computed off the symbol's account-net size, so two
    strategies holding offsetting legs back nothing and post nothing, where a
    per-partition Σ would report collateral against a position the venue does
    not have (ADR-0035).

    Isolated buckets are **inside** this Σ, which is what locks them out of
    ``free_margin`` below (ADR-0040 §7)."""
    total_maintenance_margin: Decimal | None
    """Σ ``maintenance_margin`` over every position — the floor the whole account
    must stay above (ADR-0041 §4).

    Reported over **all** positions, cross and isolated alike, even though the
    venue's own figure to cross-check against covers the cross subset only
    (ADR-0046 §2.1). The report is the honest number; the narrowing belongs to
    the comparison, not here."""
    free_margin: Decimal | None
    """``equity − total_margin_used`` — the pool left to open against.

    **Reported when negative, with no rejection and no liquidation** (ADR-0040
    §7). A negative value is a valid state rather than a divergence, and letting
    it through is a deliberate departure from a simulator that would refuse the
    margin-breaching order: it is the honest "you would have been rejected or
    liquidated on live" signal, which clamping at zero would replace with a
    report of solvency."""
    effective_leverage: Decimal | None
    """``total_notional / equity`` — the whole account's realized leverage
    (ADR-0041 §4).

    ``total_notional`` is not itself a field: it exists only to be divided, and
    a per-symbol exposure is already on each ``PositionView``.

    ``None`` on a non-positive equity as well as on the missing terms the Σs
    share — the one Tier-2 ``None`` a fresh mark cannot cure (ADR-0041 §6)."""


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

    def accrue_fee(self, fee: Decimal, *, event_id: str) -> None:
        """``−`` fees, the second of the four accruing inputs (ADR-0042 §4).

        **The negation is this method's whole subject.** ``fee`` arrives in the
        convention every producer states it in — ``> 0`` a cost, ``< 0`` a maker
        rebate (ADR-0036) — and cash moves the other way, so a cost debits and a
        rebate credits. Doing it here rather than at the call site is what makes
        the sign one decision instead of one per caller, and what lets this class
        answer "what may move cash, and in which direction" on its own.

        Unconditional and keyed by nothing, exactly as ``accrue_realized`` is and
        for the same reason: the fill has already passed ``Position.apply``, its
        one gatekeeper. ``event_id`` is provenance — *which fact* moved the line.
        """
        self._cash -= fee

    def accrue_funding(self, funding: Decimal, *, event_id: str) -> None:
        """``+`` funding, the third of the four accruing inputs (ADR-0042 §4).

        **The absence of a negation is this method's whole subject**, and it is
        the deliberate opposite of ``accrue_fee`` above. ``funding`` arrives
        mirroring ``userFunding.usdc`` — ``< 0`` paid out, ``> 0`` received
        (ADR-0037) — where a fee arrives mirroring ``fee``, positive being a
        cost. The two venue fields carry opposite raw signs, so the two cash
        rules must differ for the two lines to stay faithful to their own
        sources; forcing one house convention would put a flip somewhere, and
        the place it would go is live's ingest, whose value is that it reads the
        venue's number verbatim.

        Unconditional and keyed by nothing, exactly as its two siblings are.
        The gatekeeper here is neither of theirs: an accrual is deduped by the
        durable per-symbol watermark at ``(symbol, boundary_ts)`` grain
        (ADR-0043 §5.2), which is the one key ``Position.apply``'s
        ``event_id`` set could never stand in for — funding has no fill to be
        applied to. ``event_id`` is provenance, not a key.
        """
        self._cash += funding

    def correct_cash(self, target: Decimal, *, event_id: str) -> None:
        """Set the cash line to venue truth — the standing **correction**, and
        the one writer here that is not one of the four accruing inputs
        (ADR-0042 §4, ADR-0034).

        **A target, not a delta**, and that is the method's whole subject. The
        heal exists precisely because the four inputs failed to reproduce the
        venue's number, so a delta would have to be computed against a reading of
        this line taken *before* the same cycle's size heals moved it — and a
        size heal that closes into a partition realizes PnL, which accrues here
        on the way past. Assigning absorbs that by construction: whatever the
        fold did to the line in between, the line ends at the figure the venue
        implies. A delta would double-count the realized leg or, computed after,
        chase it.

        Genesis is deliberately untouched. The opening declaration is the
        account's identity and is written once (ADR-0042 §3): moving it to keep
        ``cash − genesis`` looking like the engine's own accruals would erase the
        record that a correction happened at all, which is the auditability the
        heal exists for.

        Unconditional and keyed by nothing, as the accruals are. The gatekeeper
        is the cycle: it emits one correction per pass, having compared against
        one fold. ``event_id`` is provenance — *which fact* moved the line — and
        applying the same one twice is a re-assignment to the same target rather
        than a second effect, so idempotency is a property of the verb here
        rather than of a key.
        """
        self._cash = target

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

        ``genesis = venue_cash(state)``, and **the subtraction that function
        performs is load-bearing rather than tidy**: the venue's equity figure
        already contains unrealized PnL, so writing it straight into the cash
        line would double-count that PnL the instant ``equity = cash + Σ uPnL``
        (ADR-0040 §7) was evaluated — an account opened holding a position would
        report its uPnL twice from the very first cycle. The live reconcile
        cycle compares against that same function, which is why the derivation
        is shared rather than restated here.

        Driven once, at the startup barrier, and only when the store holds no
        account row (ADR-0043 §6): the value is **provenance only** on this path
        — nothing cross-checks it, because there is no configured counterpart to
        check against — so re-deriving it on a later start would silently move a
        line that is supposed to have been written once.
        """
        return cls(
            account_id=spec.account_id,
            genesis_collateral=venue_cash(state),
            genesis_ts_ns=ts_ns,
        )

    def __repr__(self) -> str:
        return (
            f"Account(account_id={self._account_id!r}, "
            f"genesis_collateral={self._genesis}, cash={self._cash})"
        )
