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
    OrderState,
    OrderStatusReport,
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


def test_a_marketable_limit_partial_fill_rests_its_remainder_and_converges() -> None:
    """A GTC LIMIT that crosses on arrival is not special-cased into a single
    full fill: the model may only partial-fill it, so the venue rests the
    remainder on the book — exactly like a resting order — and later crossing
    ticks converge it to FILLED. The arrival fill must not be lost."""
    bus = InMemoryBus()
    clock = ManualClock()
    exchange = PaperExchange(bus=bus, clock=clock, fill_model=_partial_model(fraction="0.4"))
    fills: list[FillReport] = []
    bus.subscribe(FillReport, lambda r: _record(fills, r))

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("41000"))  # below the limit: crosses on arrival
        await exchange.place(_limit_order("42000", qty="1"))  # BUY limit above market
        for _ in range(2):
            await bus.publish(_tick("41000"))

    asyncio.run(scenario())

    quantities = [f.quantity for f in fills]
    assert quantities == [Decimal("0.4"), Decimal("0.4"), Decimal("0.2")]
    assert sum(quantities, Decimal(0)) == Decimal("1")


def test_a_marketable_ioc_limit_partial_fill_cancels_its_remainder() -> None:
    """IOC never rests: a marketable IOC LIMIT that the model only partial-fills
    on arrival fills what it can now and cancels the unfilled remainder — no
    resting, and no later tick fills more."""
    bus = InMemoryBus()
    clock = ManualClock()
    exchange = PaperExchange(bus=bus, clock=clock, fill_model=_partial_model(fraction="0.4"))
    fills: list[FillReport] = []
    statuses: list[OrderStatusReport] = []
    bus.subscribe(FillReport, lambda r: _record(fills, r))
    bus.subscribe(OrderStatusReport, lambda r: _record(statuses, r))

    ioc = PlaceOrder(
        cloid="0xioc",
        symbol="BTC",
        side=Side.BUY,
        quantity=Decimal("1"),
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.IOC,
        price=Decimal("42000"),
    )

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("41000"))  # crosses on arrival
        await exchange.place(ioc)
        await bus.publish(_tick("41000"))  # would fill more if it had rested

    asyncio.run(scenario())

    assert [f.quantity for f in fills] == [Decimal("0.4")]  # only the arrival fill
    assert [s.status for s in statuses] == [OrderState.CANCELLED]


def test_a_queue_miss_emits_no_fill_and_leaves_the_order_resting() -> None:
    """Queue position (ADR-0012): with ``prob_fill_on_limit`` at zero the model
    always returns ``None`` on a crossing tick — someone is always ahead of us.
    No ``FillReport`` is emitted and the order stays resting, provably: a later
    cancel still finds it on the book and reports it cancelled."""
    bus = InMemoryBus()
    clock = ManualClock()
    model = StochasticFillModel(rng=random.Random(0), clock=clock, prob_fill_on_limit=0.0)
    exchange = PaperExchange(bus=bus, clock=clock, fill_model=model)
    fills: list[FillReport] = []
    statuses: list[OrderStatusReport] = []
    bus.subscribe(FillReport, lambda r: _record(fills, r))
    bus.subscribe(OrderStatusReport, lambda r: _record(statuses, r))

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("42000"))  # rests
        await exchange.place(_limit_order("41000", qty="1"))
        for _ in range(3):
            await bus.publish(_tick("41000"))  # crosses, but always a queue miss
        await exchange.cancel("0xabc")

    asyncio.run(scenario())

    assert fills == []  # never fills while it loses the queue
    assert [s.status for s in statuses] == [OrderState.LIVE, OrderState.CANCELLED]


def test_fill_latency_advances_virtual_time_via_the_injected_clock() -> None:
    """Latency is modeled by awaiting the injected Clock (ADR-0005): the model
    and exchange share one clock, so a configured latency advances virtual time
    and lands in the fill's timestamp. Under ManualClock the wait is virtual —
    the suite runs with zero real sleeps."""
    bus = InMemoryBus()
    clock = ManualClock()
    model = StochasticFillModel(rng=random.Random(0), clock=clock, latency_seconds=2.0)
    exchange = PaperExchange(bus=bus, clock=clock, fill_model=model)
    fills: list[FillReport] = []
    bus.subscribe(FillReport, lambda r: _record(fills, r))

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("42000"))
        await exchange.place(_market_order())

    asyncio.run(scenario())

    assert len(fills) == 1
    # 1_000 ns base + 2.0 s of virtual latency = 2_000_001_000 ns.
    assert fills[0].ts_event == 1_000 + 2_000_000_000


def _run_stream(seed: int) -> list[tuple[Decimal, Decimal]]:
    """Drive a mixed slippage+partial scenario end-to-end and return the raw
    (quantity, price) of every FillReport in order — the venue's fill sequence."""
    bus = InMemoryBus()
    clock = ManualClock()
    model = StochasticFillModel(
        rng=random.Random(seed),
        clock=clock,
        prob_slippage=1.0,
        max_slippage=Decimal("0.001"),
        prob_fill_on_limit=1.0,
        partial_fill_fraction=Decimal("0.4"),
    )
    exchange = PaperExchange(bus=bus, clock=clock, fill_model=model)
    fills: list[FillReport] = []
    bus.subscribe(FillReport, lambda r: _record(fills, r))

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("42000"))
        await exchange.place(_market_order(cloid="0xm1"))  # slipped market fill
        await exchange.place(_limit_order("41000", cloid="0xl1"))  # rests
        for _ in range(3):
            await bus.publish(_tick("41000"))  # partials converge
        await exchange.place(_market_order(cloid="0xm2"))  # another slipped fill

    asyncio.run(scenario())
    return [(f.quantity, f.price) for f in fills]


def test_same_seed_and_tick_stream_replays_a_byte_identical_fill_sequence() -> None:
    """The headline determinism guarantee (ADR-0012): a fixed seed over the same
    tick stream reproduces the whole fill sequence exactly — slippage prices and
    partial quantities alike — while a different seed diverges."""
    assert _run_stream(11) == _run_stream(11)  # replayable to the byte
    assert _run_stream(11) != _run_stream(22)  # the seed is what makes it differ
