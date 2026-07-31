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
when a test only wants a read to return ``None``.

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

    async def stop(self) -> None:
        await self._venue.stop()

    async def place(self, order: PlaceOrder) -> None:
        await self._venue.place(order)

    async def cancel(self, cloid: str) -> None:
        await self._venue.cancel(cloid)

    async def fetch_order(self, cloid: str) -> VenueOrderView | None:
        return await self._venue.fetch_order(cloid)

    async def fetch_account_state(self) -> VenueAccountState | None:
        return await self._venue.fetch_account_state()

    def account_spec(self) -> AccountSpec:
        return self._venue.account_spec()

    def instrument_specs(self) -> Mapping[str, InstrumentSpec]:
        return self._venue.instrument_specs()
