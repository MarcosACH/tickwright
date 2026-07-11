"""``StochasticFillModel`` — the seeded second ``FillModel`` (ADR-0012).

A seeded RNG and the injected ``Clock`` make every fill decision deterministic:
same seed + same tick stream ⇒ byte-identical fills; different seed ⇒ a different
sequence. Slippage, queue position, partial fills, and latency are the four
nondeterminism-shaped decisions it owns behind the same interface the default
``ImmediateFillModel`` satisfies — so realism stays a wiring choice.
"""

import asyncio
import random
from decimal import Decimal

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.paper import PaperExchange, StochasticFillModel
from tickwright.domain import (
    FillReport,
    MarketTick,
    OrderType,
    PlaceOrder,
    Side,
    TimeInForce,
)


def _tick(price: str, ts: int = 1_000, symbol: str = "BTC") -> MarketTick:
    from tickwright.domain import AggressorSide

    return MarketTick(
        ts_event=ts,
        ts_init=ts,
        symbol=symbol,
        price=Decimal(price),
        size=Decimal("5"),
        aggressor_side=AggressorSide.BUY,
        trade_id=f"t{ts}",
        seq=0,
    )


def _market_order(qty: str = "1", symbol: str = "BTC", cloid: str = "0xabc") -> PlaceOrder:
    return PlaceOrder(
        cloid=cloid,
        symbol=symbol,
        side=Side.BUY,
        quantity=Decimal(qty),
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.IOC,
    )


def _limit_order(
    price: str, *, qty: str = "1", cloid: str = "0xabc", symbol: str = "BTC"
) -> PlaceOrder:
    return PlaceOrder(
        cloid=cloid,
        symbol=symbol,
        side=Side.BUY,
        quantity=Decimal(qty),
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        price=Decimal(price),
    )


def _model(seed: int) -> StochasticFillModel:
    # prob_slippage=1.0 so the seed's draw is always observable in the price.
    return StochasticFillModel(
        rng=random.Random(seed),
        clock=ManualClock(),
        prob_slippage=1.0,
        max_slippage=Decimal("0.001"),
    )


def test_same_seed_gives_byte_identical_market_fills() -> None:
    order = _market_order()
    tick = _tick("42000")

    first = asyncio.run(_model(7).market_fill(order, tick))
    again = asyncio.run(_model(7).market_fill(order, tick))

    assert first == again  # same seed + same tick ⇒ byte-identical fill


def test_a_different_seed_gives_a_different_market_fill() -> None:
    order = _market_order()
    tick = _tick("42000")

    seven = asyncio.run(_model(7).market_fill(order, tick))
    ninety_nine = asyncio.run(_model(99).market_fill(order, tick))

    assert seven != ninety_nine  # the seed is what makes the sequence differ


def test_slippage_is_adverse_and_within_the_configured_bound() -> None:
    tick = _tick("40000")
    price = Decimal("40000")
    ceiling = price * (Decimal(1) + Decimal("0.001"))
    floor = price * (Decimal(1) - Decimal("0.001"))

    # Sweep many seeds: a BUY never fills below the market and never worse than
    # the bound; a SELL is the mirror. Slippage is a cost, never a windfall.
    for seed in range(50):
        buy = asyncio.run(_model(seed).market_fill(_market_order(), tick))
        assert price <= buy.price <= ceiling

        sell = StochasticFillModel(
            rng=random.Random(seed),
            clock=ManualClock(),
            prob_slippage=1.0,
            max_slippage=Decimal("0.001"),
        )
        sell_fill = asyncio.run(
            sell.market_fill(
                PlaceOrder(
                    cloid="0xsell",
                    symbol="BTC",
                    side=Side.SELL,
                    quantity=Decimal("1"),
                    order_type=OrderType.MARKET,
                    time_in_force=TimeInForce.IOC,
                ),
                tick,
            )
        )
        assert floor <= sell_fill.price <= price


async def _record(sink: list, report: object) -> None:
    sink.append(report)


def _partial_model(*, fraction: str) -> StochasticFillModel:
    # No slippage, always fills on a crossing tick (queue miss lands later);
    # each crossing fills ``fraction`` of the original quantity.
    return StochasticFillModel(
        rng=random.Random(0),
        clock=ManualClock(),
        prob_fill_on_limit=1.0,
        partial_fill_fraction=Decimal(fraction),
    )


def test_a_resting_limit_partial_fills_across_ticks_and_converges() -> None:
    """A GTC LIMIT that rests, then is crossed by successive ticks, fills a
    fraction of its remainder each tick — a sequence of ``FillReport``s whose
    cumulative quantity is monotonic and converges to exactly the order size.
    The exchange caps each fill to the remaining, so it never over-fills."""
    bus = InMemoryBus()
    clock = ManualClock()
    exchange = PaperExchange(bus=bus, clock=clock, fill_model=_partial_model(fraction="0.4"))
    fills: list[FillReport] = []
    bus.subscribe(FillReport, lambda r: _record(fills, r))

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("42000"))  # above the limit: order rests, no cross
        await exchange.place(_limit_order("41000", qty="1"))
        # Three successive crossing ticks: 0.4 + 0.4 + 0.2(capped) = 1.0.
        for _ in range(3):
            await bus.publish(_tick("41000"))

    asyncio.run(scenario())

    quantities = [f.quantity for f in fills]
    assert quantities == [Decimal("0.4"), Decimal("0.4"), Decimal("0.2")]

    cumulative = [sum(quantities[: i + 1], Decimal(0)) for i in range(len(quantities))]
    assert cumulative == sorted(cumulative)  # monotonic
    assert cumulative[-1] == Decimal("1")  # converges to the full order size
