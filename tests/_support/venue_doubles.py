"""The ``Exchange`` seam's ceremony, for suites whose subject is not the venue.

A venue double exists to make one thing happen — a read that fails, a placement
that dies mid-send, a link that drops — and the seam makes it implement six
other members to say so. Across the suite roughly a fifth of the members written
on doubles carry behaviour; the rest are there to typecheck, and every widening
of ``Exchange`` (four more land with the trade-economics surface) writes another
round of them into files whose subject is not the venue at all.

Since the seam split into its two anchors (``domain.OrderAnchor`` /
``domain.AccountAnchor``), that cost is paid **only by a case whose subject is
the whole venue** — a runner driving a lifecycle, a link standing in front of a
real adapter. A suite whose subject is one grain doubles that grain and stops:
the account cycle's own double is two members and no base class
(``tests/engine/test_ledger_reconcile.py``). These bases are for the rest, and
they are the reason the widening above is still worth holding in one place
rather than the reason the ceremony exists.

These two bases hold that ceremony once, and they hold it differently because a
double either invents the seam's answers or borrows a real venue's.
``VenueDouble`` deliberately does **not** hold ``place``/``cancel``/
``fetch_order``: those carry each double's meaning — including the
assertion-raisers, whose messages *are* the specification ("nothing may be
placed before the barrier clears") — so they stay in the suite that asserts
them. A base that absorbed those would move the specification away from the test
and leave the ceremony behind, which is exactly backwards. ``VenueLink`` does
hold all three, delegating them to the venue it wraps, so a subclass overriding
one is *replacing an answer* rather than adding a member — the meaning still
lives in the suite, and the members it does not name stay the real venue's.

Why bases and not subclasses of the real ``PaperExchange``, as every ``Store``
double subclasses ``SQLiteStore``: the paper venue subscribes itself to
``MarketTick`` at construction (ADR-0012), so it needs a bus and a clock even
when a test only wants a read to fail.

Doubling here is legitimate under ADR-0022 — a venue is a process boundary, the
one place a double is allowed.
"""

from collections.abc import Iterable, Mapping
from dataclasses import replace
from decimal import Decimal

from ledgers import GENESIS

from tickwright.domain import (
    DEFAULT_LEVERAGE,
    AccountModeVerdict,
    AccountSpec,
    Exchange,
    InstrumentSpec,
    PlaceOrder,
    VenueAccountState,
    VenueOrderView,
    VenuePositionState,
    VenueReadFailure,
)

_ZERO = Decimal("0")

RECORDED_ENTRY_PRICE = Decimal("64809.0")
"""The recorded snapshot's own entry price, and the default every derived leg
is priced from — named because a roster the case chose the sizes of has to be
able to say it entered somewhere else."""


def implied_notional(signed_size: Decimal, entry_price: Decimal, unrealized: Decimal) -> Decimal:
    """The ``positionValue`` a venue holding this position would publish.

    ``|szi| × mark``, with the mark the position's own numbers imply: a long
    entered at ``entry`` and carrying ``uPnL`` is marked at ``entry + uPnL/szi``,
    so the exposure is ``|szi| × entry`` plus the open PnL — less it on a short,
    where the same profit means a mark that has fallen.

    The recorded constant it replaces is the arithmetic of the snapshot it came
    from and of nothing a suite builds beside it: 129.584 is 0.002 BTC at 64809
    carrying −0.034, which every case that marks its book differently then
    contradicts. Inert until #291 compared the field; compared, it is a
    ``NOTIONAL`` divergence in every case that says nothing about exposure —
    ``implied_free_margin``'s lesson on the field the band is scaled by.

    No ``declared`` escape hatch beside that one's, and the asymmetry is the
    point: a free margin the venue disagrees on is a snapshot it could have
    returned, while a ``positionValue`` that contradicts the size, entry and
    uPnL published beside it is not. A case that wants an exposure disagreement
    moves the leg's ``entry_price`` or its uPnL and gets one that adds up.
    """
    exposure = abs(signed_size) * entry_price
    return exposure + unrealized if signed_size > 0 else exposure - unrealized


def margined(position: VenuePositionState) -> VenuePositionState:
    """``position`` with the ``marginUsed`` a venue holding it would publish.

    Each mode's own identity, read off the row's own fields (ADR-0040 §3): a
    cross position posts ``positionValue / L`` out of the account pool, and an
    isolated one posts the bucket it locked, marked to market — its ``collateral
    + uPnL``. The two differ at every mark but the entry, so a fixture answering
    one of them for both is a snapshot no venue returns.

    Applied by every constructor and every mutator that moves one of those
    inputs, rather than once at construction, because a leverage or a bucket set
    afterwards changes the figure: ``margin_used`` is the third field of this
    family (after ``free_margin`` and ``notional``) whose recorded constant went
    stale the moment ADR-0040 §6 compared it, and it is the first that a *later*
    replacement can invalidate.

    **A row that answers the two mode questions differently is refused rather
    than priced**, and the refusal is what makes the rest of this trustworthy.
    ``VenuePositionState`` carries the mode twice — ``isolated_collateral`` is
    ``None`` *exactly* when the position is cross and backed by the account
    pool, while ``leverage`` is the venue's stored setting — so either half
    alone picks the rule, and a row where they disagree has two answers and no
    reason to prefer one. Read off the leverage with the bucket defaulted to
    ``0``, as this used to be, the isolated arm publishes the bare unrealized
    PnL wearing the bucket's name: a **negative** ``marginUsed`` on any losing
    position, which is not a number a venue returns. Inventing the missing
    bucket cost nothing to write, so every constructor below had to be *trusted*
    to keep the pair in step; refused, an incoherent row cannot reach a
    comparison at all.
    """
    bucket = position.isolated_collateral
    if position.leverage.mode == "cross":
        if bucket is not None:
            raise ValueError(
                f"{position.symbol}: a cross row is backed by the account pool and posts no "
                f"bucket of its own, got isolated_collateral={bucket}"
            )
        return replace(position, margin_used=position.notional / position.leverage.leverage)
    if bucket is None:
        raise ValueError(
            f"{position.symbol}: an isolated row has to declare the bucket its margin is "
            "computed from, got isolated_collateral=None, which is how a venue says cross"
        )
    return replace(position, margin_used=bucket + position.unrealized_pnl)


def implied_free_margin(equity: str, unrealized: Iterable[str], *, declared: str | None) -> Decimal:
    """The free margin a venue holding these positions would publish.

    ``equity − Σ uPnL``, and it is the *ledger's* default margin mode that makes
    it so: at isolated 1x an account's margin is its positions' own buckets
    marked to market, so ``free_margin = equity − total_margin_used`` collapses
    to the venue's equity less the open PnL already inside it.

    A recorded constant in its place is coherent with the account it was
    recorded from and with nothing a suite builds beside it — the snapshot's
    ``0.0096`` belongs to a 5x cross book — which made every case that marks a
    book diverge on a field it says nothing about, now that ADR-0040 §6 compares
    this figure at Tier-2.

    One derivation because two fixtures need it, the recorded snapshot and the
    explicit roster. Spelled twice they would agree until the first case that
    varied one of them, and a cycle reading a divergence off the difference
    would be reporting the fixtures disagreeing rather than the book.

    ``declared`` takes precedence and is the escape hatch: a case whose ledger
    runs at some *other* leverage, or that wants the disagreement, passes the
    figure that account would publish — the default's premise is the default's
    leverage.
    """
    if declared is not None:
        return Decimal(declared)
    return Decimal(equity) - sum((Decimal(pnl) for pnl in unrealized), Decimal("0"))


UNPOSTED_BUCKET = Decimal("0")
"""The isolated bucket this fixture's rows declare, and the one figure here that
is the **ledger's** shape rather than a venue's.

A real venue's isolated position always locks a positive bucket, so a venue
returning this row is not what it models. What it models is the pre-ingest state
the cadence actually compares against: live never computes the bucket
(``_lock_isolated_collateral`` declines on the declared-versus-ingested
predicate), the reading is taken *before* the pass ingests it, and our own
``Position.isolated_collateral`` opens at the ``0`` the dataclass gives it. So a
ledger holding one of these symbols computes ``0 + uPnL``, and the row that
agrees with it is this one.

Declared rather than left at ``None``, which is what it used to be, and the
difference is the point: ``None`` is how ``VenuePositionState`` says **cross**,
so the row claimed a cross bucket beside an isolated ``leverage`` and ``margined``
resolved the contradiction by inventing the zero. Written down, the same numbers
come out of a row whose two mode signals agree, and ``margined`` can refuse the
ones that do not.

A case whose subject *is* the bucket posts a real one with ``_isolated`` and gets
the ``MARGIN_USED`` divergence a first cycle of a life genuinely reports."""


CROSSLESS_MAINTENANCE = Decimal("0")
"""``crossMaintenanceMarginUsed`` on a book holding no cross position.

The venue's field is **cross-scoped** and an isolated leg contributes nothing to
it (ADR-0046 §2.1, measured: an account of one isolated position publishes
``0.0``), and both fixtures below build their rows at the ledger's default
isolated 1x — so this, and not the recorded snapshot's figure, is what a venue
holding them returns.

The recorded ``1.6198`` it replaces is the *cross 5x* account it was measured
from, and it is the fourth constant of this family to go stale the moment its
field was compared, after ``free_margin``, ``notional`` and ``margin_used``.
Unlike those three there is nothing here to derive it from: maintenance is
``notional × margin_maint`` and the rate lives on an ``InstrumentSpec`` no venue
snapshot carries. So a case running a **cross** book declares the figure its own
account would publish — which ``_levered`` cannot do for it, for the same
reason."""


def account_state(
    equity: str,
    *unrealized: str,
    free_margin: str | None = None,
    maintenance: str | None = None,
) -> VenueAccountState:
    """A successful venue account read holding one position per ``unrealized``.

    The figures are the recorded cross snapshot's (issue #142 §2, reproduced in
    full in ``tests/venues/hyperliquid/test_account.py``): a funded testnet
    account holding 0.002 BTC long at 5x, ``accountValue`` 25.9264 against an
    unrealized −0.034. Only ``equity``, the unrealized legs and ``free_margin``
    carry meaning for a caller — the first two are what ADR-0042 §6's genesis
    formula reads and the third is now cross-checked (ADR-0040 §6) — but the
    rest are a real venue's own numbers, so what a suite hands the seam is a
    shape the venue could have returned rather than one invented to fit.

    ``free_margin`` and each leg's ``notional`` are derived from ``equity`` and
    the legs rather than kept at their recorded constants, on the premises
    ``implied_free_margin`` and ``implied_notional`` state; the first takes a
    case's own figure where it wants the disagreement, the second is the
    snapshot's own arithmetic and takes nothing.

    ``maintenance`` is the third that had to move off its recorded constant, on
    ``CROSSLESS_MAINTENANCE``'s premise: this fixture's rows are isolated, and
    the venue's field counts only cross ones.

    Those rows are isolated in **both** places that say so — an
    ``UNPOSTED_BUCKET`` beside the isolated ``leverage`` — so ``margined`` prices
    them off a bucket the row declares rather than one it invents.
    """
    return VenueAccountState(
        equity=Decimal(equity),
        free_margin=implied_free_margin(equity, unrealized, declared=free_margin),
        cross_maintenance_margin=(
            CROSSLESS_MAINTENANCE if maintenance is None else Decimal(maintenance)
        ),
        positions=tuple(
            margined(
                VenuePositionState(
                    symbol="BTC",
                    signed_size=Decimal("0.002"),
                    entry_price=RECORDED_ENTRY_PRICE,
                    notional=implied_notional(Decimal("0.002"), RECORDED_ENTRY_PRICE, Decimal(pnl)),
                    unrealized_pnl=Decimal(pnl),
                    # Overwritten by ``margined`` — the recorded 25.9168 is
                    # ``positionValue / 5`` on the snapshot's own cross book,
                    # which is not the book a suite builds beside it.
                    margin_used=_ZERO,
                    isolated_collateral=UNPOSTED_BUCKET,
                    liquidation_price=None,
                    # The **ledger's** default pair rather than the recorded
                    # body's ``cross 5``, on the same premise
                    # ``implied_free_margin`` states: this fixture's account is
                    # one a suite builds at isolated 1x, and a venue setting
                    # that disagreed with it would put a standing
                    # ``LEVERAGE_DIVERGENCE`` under every case here that says
                    # nothing about leverage (ADR-0044 §10). A case about drift
                    # overrides it.
                    leverage=DEFAULT_LEVERAGE,
                )
            )
            for pnl in unrealized
        ),
    )


class VenueDouble:
    """The ``Exchange`` members no double varies.

    Subclasses add ``place``, ``cancel`` and ``fetch_order`` — the three that
    say what the double is *for* — and inherit the rest. The declarations here
    are the paper venue's, matching ``ledgers.py``'s account so a double and the
    ledger a test wires beside it agree on which account they are talking about.
    """

    async def start(self) -> None:
        return None

    async def run(self) -> None:
        # No loop to supervise: a double models a venue's *answers*, and the one
        # adapter with a long-lived half of its own is the real paper venue's
        # funding generator. The runner still task-creates this; it completes.
        return None

    async def stop(self) -> None:
        return None

    async def fetch_account_state(self) -> VenueAccountState | None:
        # The paper venue's permanent answer, and the fail-closed one for a
        # double as well: no account truth to compare against, so nothing heals
        # (ADR-0011 inv 1). A double that needs a venue account read to *succeed*
        # overrides this, which is the seam's meaning arriving in the suite that
        # asserts it rather than being inherited from here.
        return None

    async def verify_account_mode(self) -> AccountModeVerdict:
        # The paper venue's permanent answer again: nothing to verify, rather
        # than nothing verified. A double asserting on the guard says so by
        # overriding this — the refusal is a case's meaning, not ceremony.
        return AccountModeVerdict.VERIFIED

    def account_spec(self) -> AccountSpec:
        return AccountSpec(account_id="paper-default", genesis_collateral=GENESIS)

    def instrument_specs(self) -> Mapping[str, InstrumentSpec]:
        return {}


DERIVED_STATE = account_state("25.9264", "-0.034")
"""The healthy account read a live double answers unless a case says otherwise."""

LIVE_ACCOUNT_ID = "hyperliquid-testnet-0xabc"
"""Three segments where paper's is two (ADR-0038/0042 §5), so a row written by
one shape is never confusable with the other's."""

DERIVED_GENESIS = Decimal("25.9604")
"""What ``account_state``'s default figures imply: ``25.9264 − (−0.034)``, the
venue's own arithmetic rather than the engine's restated (ADR-0042 §6)."""


class LiveVenueDouble(VenueDouble):
    """The same ceremony in the **live** shape: a genesis the venue reports.

    ``VenueDouble`` declares the paper account — a genesis in hand and no
    account truth to read — and that is the wrong shape for every case about the
    startup barrier's materialisation, where the point is precisely that the
    opening balance is *ingested* (ADR-0042 §6). The two members that say so
    live here rather than in each suite; ``place``/``cancel``/``fetch_order``
    still do not, because those carry each double's meaning.

    ``state`` is what the venue answers the account read with, and ``None`` is a
    **failed read** — the outage that must fault the barrier rather than clear
    it — so it is a default rather than a fallback resolved inside ``__init__``:
    a double that quietly swapped ``None`` for the healthy answer could not model
    the case at all. ``account_reads`` is public because "how many times was the
    venue asked" is the assertion for both a first start (once) and a restart
    (never).
    """

    def __init__(self, *, state: VenueAccountState | None = DERIVED_STATE) -> None:
        self.account_reads = 0
        self._state = state

    def account_spec(self) -> AccountSpec:
        return AccountSpec(account_id=LIVE_ACCOUNT_ID, genesis_collateral=None)

    async def fetch_account_state(self) -> VenueAccountState | None:
        self.account_reads += 1
        return self._state


class VenueLink:
    """A link in front of a **real** venue, delegating the whole seam.

    The subclass overrides the one member whose failure it models — a placement
    that dies mid-send, a read that drops — and everything else stays the real
    venue's answer rather than a stub the suite would have to keep true.
    """

    def __init__(self, venue: Exchange) -> None:
        self._venue = venue

    async def start(self) -> None:
        await self._venue.start()

    async def run(self) -> None:
        await self._venue.run()

    async def stop(self) -> None:
        await self._venue.stop()

    async def place(self, order: PlaceOrder) -> None:
        await self._venue.place(order)

    async def cancel(self, cloid: str) -> None:
        await self._venue.cancel(cloid)

    async def fetch_order(self, cloid: str) -> VenueOrderView | VenueReadFailure:
        return await self._venue.fetch_order(cloid)

    async def fetch_account_state(self) -> VenueAccountState | None:
        return await self._venue.fetch_account_state()

    async def verify_account_mode(self) -> AccountModeVerdict:
        return await self._venue.verify_account_mode()

    def account_spec(self) -> AccountSpec:
        return self._venue.account_spec()

    def instrument_specs(self) -> Mapping[str, InstrumentSpec]:
        return self._venue.instrument_specs()
