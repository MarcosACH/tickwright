"""The ``Exchange`` seam's ceremony, for suites whose subject is not the venue.

A venue double exists to make one thing happen — a read that fails, a placement
that dies mid-send, a link that drops — and the seam makes it implement six
other members to say so. Across the suite roughly a fifth of the members written
on doubles carry behaviour; the rest are there to typecheck, and every widening
of ``Exchange`` (four more land with the trade-economics surface) writes another
round of them into files whose subject is not the venue at all.

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

from collections.abc import Mapping
from decimal import Decimal

from ledgers import GENESIS

from tickwright.domain import (
    AccountSpec,
    Exchange,
    InstrumentSpec,
    PlaceOrder,
    VenueAccountState,
    VenueOrderView,
    VenuePositionState,
    VenueReadFailure,
)


def account_state(equity: str, *unrealized: str) -> VenueAccountState:
    """A successful venue account read holding one position per ``unrealized``.

    The figures are the recorded cross snapshot's (issue #142 §2, reproduced in
    full in ``tests/venues/hyperliquid/test_account.py``): a funded testnet
    account holding 0.002 BTC long at 5x, ``accountValue`` 25.9264 against an
    unrealized −0.034. Only ``equity`` and the unrealized legs carry meaning for
    a caller — they are what ADR-0042 §6's genesis formula reads — but the rest
    are a real venue's own numbers, so what a suite hands the seam is a shape
    the venue could have returned rather than one invented to fit.
    """
    return VenueAccountState(
        equity=Decimal(equity),
        free_margin=Decimal("0.0096"),
        cross_maintenance_margin=Decimal("1.6198"),
        positions=tuple(
            VenuePositionState(
                symbol="BTC",
                signed_size=Decimal("0.002"),
                entry_price=Decimal("64809.0"),
                notional=Decimal("129.584"),
                unrealized_pnl=Decimal(pnl),
                margin_used=Decimal("25.9168"),
                isolated_collateral=None,
                liquidation_price=None,
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

    def account_spec(self) -> AccountSpec:
        return self._venue.account_spec()

    def instrument_specs(self) -> Mapping[str, InstrumentSpec]:
        return self._venue.instrument_specs()
