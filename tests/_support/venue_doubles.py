"""The ``Exchange`` seam's ceremony, for suites whose subject is not the venue.

A venue double exists to make one thing happen — a read that fails, a placement
that dies mid-send, a link that drops — and the seam makes it implement six
other members to say so. Across the suite roughly a fifth of the members written
on doubles carry behaviour; the rest are there to typecheck, and every widening
of ``Exchange`` (four more land with the trade-economics surface) writes another
round of them into files whose subject is not the venue at all.

These two bases hold that ceremony once. What they deliberately do **not** hold
is ``place``/``cancel``/``fetch_order``: those carry each double's meaning —
including the assertion-raisers, whose messages *are* the specification ("nothing
may be placed before the barrier clears") — so they stay in the suite that
asserts them. A base that absorbed those would move the specification away from
the test and leave the ceremony behind, which is exactly backwards.

Why bases and not subclasses of the real ``PaperExchange``, as every ``Store``
double subclasses ``SQLiteStore``: the paper venue subscribes itself to
``MarketTick`` at construction (ADR-0012), so it needs a bus and a clock even
when a test only wants a read to return ``None``.

Doubling here is legitimate under ADR-0022 — a venue is a process boundary, the
one place a double is allowed.
"""

from collections.abc import Mapping

from ledgers import GENESIS

from tickwright.domain import (
    AccountSpec,
    Exchange,
    InstrumentSpec,
    PlaceOrder,
    VenueOrderView,
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

    def account_spec(self) -> AccountSpec:
        return self._venue.account_spec()

    def instrument_specs(self) -> Mapping[str, InstrumentSpec]:
        return self._venue.instrument_specs()
