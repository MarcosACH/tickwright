"""The ``MarketFeed`` contract's own red case: a feed that owes a mark and never pays it.

Every other caller of ``feed_contract`` is a *real* adapter that satisfies the
obligation, so every other caller is green — which is exactly the shape of a
contract that asserts nothing. This module drives the omission on purpose, with
a stub that satisfies the Protocol and publishes trades only, and asserts the
contract refuses it. Without this, the two adapter suites below could both be
passing a function whose body was deleted.
"""

import asyncio
from decimal import Decimal

import pytest
from feed_contract import assert_every_traded_symbol_is_marked, record_market_data

from tickwright.adapters.bus import InMemoryBus
from tickwright.domain import AggressorSide, EventBus, MarketFeed, MarketTick


class TradesOnlyFeed:
    """A ``MarketFeed`` that publishes trades and no mark at all.

    The third-venue omission #227 exists to catch, written out: it satisfies the
    Protocol, type-checks, runs, and — before the contract — passed a suite of
    its own, while ``PortfolioProjection._marks`` stayed empty and every
    ``unrealized_pnl`` / ``notional`` / ``equity`` downstream read ``None``
    forever with nothing red anywhere (ADR-0034/0039).
    """

    def __init__(self, *, bus: EventBus, symbols: list[str]) -> None:
        self._bus = bus
        self._symbols = symbols

    async def start(self) -> None:
        for seq, symbol in enumerate(self._symbols):
            await self._bus.publish(
                MarketTick(
                    ts_event=1_000 + seq,
                    ts_init=1_000 + seq,
                    symbol=symbol,
                    price=Decimal("42000"),
                    size=Decimal("1"),
                    aggressor_side=AggressorSide.BUY,
                    trade_id=f"t{seq}",
                    seq=seq,
                )
            )

    async def stop(self) -> None:
        """Nothing held open — the omission under test is the mark, not a leak."""


def test_a_feed_that_never_publishes_a_mark_fails_the_contract() -> None:
    """The obligation, stated negatively: trades for BTC and ETH, marks for
    neither, and the contract names **both** rather than the first one it hit.

    The ``isinstance`` line is not incidental — it is the ticket's premise. A
    trades-only feed *is* a ``MarketFeed`` by every check the repo had before
    this contract, which is why the omission needed a behavioural assertion
    rather than a member-grained one (``tests/_support/seam_claims.py``).
    """
    bus = InMemoryBus()
    transcript = record_market_data(bus)
    feed = TradesOnlyFeed(bus=bus, symbols=["BTC", "ETH"])

    assert isinstance(feed, MarketFeed)

    asyncio.run(feed.start())

    with pytest.raises(AssertionError, match=r"\['BTC', 'ETH'\]"):
        assert_every_traded_symbol_is_marked(transcript, feed="TradesOnlyFeed")


def test_a_run_that_produced_no_trades_at_all_cannot_satisfy_the_contract() -> None:
    """The second vacuity, and the one that will actually bite a venue author.

    "Every traded symbol is marked" is trivially true of a run that traded
    nothing, so a driver wired up wrong — subscribed after the feed ran, stopped
    before the first frame, pointed at an empty fixture — proves the obligation
    by proving nothing, and does it *green*. The contract refuses an empty
    transcript for the same reason it refuses a mark-less one: it is evidence or
    it is noise.

    Not symmetric with marks, which are left to the assertion proper: a live
    venue really does publish ``activeAssetCtx`` for a symbol that has not
    traded, so a mark-only transcript is a fact about the venue, while a
    trade-less one is only ever a fact about the driver.
    """
    bus = InMemoryBus()
    transcript = record_market_data(bus)

    with pytest.raises(AssertionError, match="no trades"):
        assert_every_traded_symbol_is_marked(transcript, feed="TradesOnlyFeed")
