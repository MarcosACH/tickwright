"""``ConflatingIngress`` — the keep-latest-per-symbol ingress buffer (ADR-0023).

The subtlest concurrency in the venue package — a reader offering ticks while a
publisher drains to the bus, conflating under backpressure — tested directly
against a real ``InMemoryBus`` and a subscribed consumer, no WebSocket. The
process boundary (the WS) is not involved; this is the buffer's own contract.
"""

import asyncio
from decimal import Decimal

from structlog.typing import EventDict

from tickwright.adapters.bus import InMemoryBus
from tickwright.domain import AggressorSide, MarketTick
from tickwright.observability.testing import capture_events
from tickwright.venues.hyperliquid.ingress import ConflatingIngress


def _tick(symbol: str, price: str, trade_id: str) -> MarketTick:
    return MarketTick(
        symbol=symbol,
        price=Decimal(price),
        size=Decimal("1"),
        aggressor_side=AggressorSide.BUY,
        trade_id=trade_id,
        seq=0,
        venue_trade_id=True,
        ts_event=1_700_000_000_000_000_000,
        ts_init=1_700_000_000_000_000_000,
    )


def test_drains_offered_ticks_to_the_bus_then_returns_when_closed() -> None:
    """A keeping-up consumer sees every offered tick, in offer order, and the
    drain loop returns once the buffer is empty and the ingress is closed."""

    async def main() -> list[MarketTick]:
        bus = InMemoryBus()
        seen: list[MarketTick] = []

        async def record(tick: MarketTick) -> None:
            seen.append(tick)

        bus.subscribe(MarketTick, record)
        ingress = ConflatingIngress(bus=bus)
        drain = asyncio.create_task(ingress.drain())
        for tick in (_tick("BTC", "100", "1"), _tick("ETH", "50", "2"), _tick("BTC", "101", "3")):
            ingress.offer(tick)
            await asyncio.sleep(0)  # let the publisher keep up, no backpressure
        ingress.close()
        await asyncio.wait_for(drain, timeout=2)
        return seen

    seen = asyncio.run(main())
    assert [(t.symbol, t.price) for t in seen] == [
        ("BTC", Decimal("100")),
        ("ETH", Decimal("50")),
        ("BTC", Decimal("101")),
    ]


def test_close_on_an_empty_buffer_ends_the_drain() -> None:
    async def main() -> None:
        ingress = ConflatingIngress(bus=InMemoryBus())
        drain = asyncio.create_task(ingress.drain())
        await asyncio.sleep(0)  # drain parks on the wake
        ingress.close()
        await asyncio.wait_for(drain, timeout=2)

    asyncio.run(main())  # returns iff close() unblocks the parked drain


def test_backpressure_keeps_only_the_latest_per_symbol_and_names_each_drop() -> None:
    """A stuck first publish; three more ticks arrive meanwhile. BTC 101 is
    superseded by BTC 102 (one drop → one feed.lagged), ETH 50 is kept. This is
    the slow-consumer contract of ADR-0023, exercised without a WebSocket."""

    async def main() -> tuple[list[MarketTick], list[EventDict]]:
        bus = InMemoryBus()
        seen: list[MarketTick] = []
        release = asyncio.Event()
        first_delivered = asyncio.Event()

        async def slow_consumer(tick: MarketTick) -> None:
            seen.append(tick)
            if len(seen) == 1:
                first_delivered.set()
                await release.wait()

        bus.subscribe(MarketTick, slow_consumer)
        ingress = ConflatingIngress(bus=bus)

        with capture_events() as logs:
            drain = asyncio.create_task(ingress.drain())
            ingress.offer(_tick("BTC", "100", "1"))
            await asyncio.wait_for(first_delivered.wait(), timeout=2)
            # The publish is stuck in the slow consumer; fold three more in.
            ingress.offer(_tick("BTC", "101", "2"))
            ingress.offer(_tick("BTC", "102", "3"))  # supersedes 101 → one drop
            ingress.offer(_tick("ETH", "50", "4"))
            release.set()

            async def rest_published() -> None:
                while len(seen) < 3:
                    await asyncio.sleep(0)

            await asyncio.wait_for(rest_published(), timeout=2)
            ingress.close()
            await asyncio.wait_for(drain, timeout=2)
        return seen, [log for log in logs if log["event"] == "feed.lagged"]

    seen, lagged = asyncio.run(main())
    assert [(t.symbol, t.price) for t in seen] == [
        ("BTC", Decimal("100")),
        ("BTC", Decimal("102")),
        ("ETH", Decimal("50")),
    ]
    assert len(lagged) == 1
    assert lagged[0]["symbol"] == "BTC"
    assert lagged[0]["dropped_trade_id"] == "2"
