"""``SingleShotMarketStrategy`` — the minimal reference strategy (ADR-0016).

It proves the ``Strategy`` seam with the thinnest possible behavior: on its first
tick it emits exactly one MARKET ``PlaceSignal`` with a deterministic, strategy-
owned ``seq``, then stays quiet; it records the ``OrderFilled`` it later receives
and the position that fill produced, read back through the ``Portfolio`` seam.
The point of every other module is that this one stays trivial to write.
"""

import asyncio
from decimal import Decimal

import pytest
from ledgers import ledger

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.domain import (
    AggressorSide,
    MarketTick,
    OrderFilled,
    OrderPlaced,
    OrderType,
    PlaceSignal,
    Side,
    Signal,
    TimeInForce,
)
from tickwright.engine.portfolio import PortfolioProjection
from tickwright.strategies import SingleShotMarketStrategy


def _tick(ts: int = 1_000, symbol: str = "BTC") -> MarketTick:
    return MarketTick(
        ts_event=ts,
        ts_init=ts,
        symbol=symbol,
        price=Decimal("42000"),
        size=Decimal("1"),
        aggressor_side=AggressorSide.BUY,
        trade_id=f"t{ts}",
        seq=0,
    )


def _harness(
    projection: PortfolioProjection | None = None,
) -> tuple[InMemoryBus, ManualClock, SingleShotMarketStrategy, list[Signal]]:
    bus = InMemoryBus()
    clock = ManualClock(start_ns=1_000)
    strategy = SingleShotMarketStrategy(
        strategy_id="trivial",
        bus=bus,
        clock=clock,
        # A real projection, scoped exactly as the composition root scopes it —
        # the strategy holds the ``domain`` Protocol and never the concrete.
        portfolio=(projection if projection is not None else ledger()).for_strategy("trivial"),
        side=Side.BUY,
        quantity=Decimal("0.5"),
    )
    bus.subscribe(MarketTick, strategy.on_tick)
    signals: list[Signal] = []
    bus.subscribe(Signal, lambda s: _record(signals, s))
    return bus, clock, strategy, signals


def test_emits_one_market_signal_on_the_first_tick() -> None:
    bus, _, _, signals = _harness()

    asyncio.run(bus.publish(_tick()))

    assert len(signals) == 1
    signal = signals[0]
    assert isinstance(signal, PlaceSignal)
    assert signal.order_type is OrderType.MARKET
    assert signal.side is Side.BUY
    assert signal.quantity == Decimal("0.5")
    assert signal.symbol == "BTC"
    assert signal.time_in_force is TimeInForce.IOC


def test_signal_id_is_deterministic_and_strategy_owned() -> None:
    bus, _, _, signals = _harness()
    asyncio.run(bus.publish(_tick()))
    assert signals[0].signal_id == "trivial:BTC:1"


def test_signal_is_stamped_from_the_clock() -> None:
    bus, clock, _, signals = _harness()

    async def scenario() -> None:
        clock.advance_to(9_000)
        await bus.publish(_tick(ts=9_000))

    asyncio.run(scenario())
    assert signals[0].ts_event == 9_000
    assert signals[0].ts_init == 9_000


def test_does_not_emit_again_on_later_ticks() -> None:
    bus, clock, _, signals = _harness()

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick(ts=1_000))
        clock.advance_to(2_000)
        await bus.publish(_tick(ts=2_000))

    asyncio.run(scenario())
    assert len(signals) == 1


def test_records_order_filled_and_ignores_other_events() -> None:
    _, _, strategy, _ = _harness()
    filled = OrderFilled(
        ts_event=2,
        ts_init=2,
        cloid="0xabc",
        strategy_id="trivial",
        signal_id="trivial:BTC:1",
        symbol="BTC",
        trade_id="v1",
        quantity=Decimal("0.5"),
        price=Decimal("42000"),
        cum_qty=Decimal("0.5"),
    )
    placed = OrderPlaced(
        ts_event=1,
        ts_init=1,
        cloid="0xabc",
        strategy_id="trivial",
        signal_id="trivial:BTC:1",
        symbol="BTC",
    )

    asyncio.run(_deliver(strategy, [placed, filled]))

    assert strategy.fills == [filled]
    # It read once, in the same handler, and only for the fill.
    assert len(strategy.positions) == 1


def test_records_the_position_it_reads_back_through_the_portfolio_seam() -> None:
    """The seam's only consumer, exercised against a real projection: the read
    is scoped to this strategy and needs no argument to say so."""
    projection = ledger()
    _, _, strategy, _ = _harness(projection)
    filled = OrderFilled(
        ts_event=2,
        ts_init=2,
        cloid="0xabc",
        strategy_id="trivial",
        signal_id="trivial:BTC:1",
        symbol="BTC",
        trade_id="v1",
        quantity=Decimal("0.5"),
        price=Decimal("42000"),
        cum_qty=Decimal("0.5"),
    )
    projection.apply_fill(filled, side=Side.BUY)

    asyncio.run(_deliver(strategy, [filled]))

    view = strategy.positions[0]
    assert view is not None
    assert view.size == Decimal("0.5")
    assert view.entry_price == Decimal("42000")


def test_reads_none_for_a_symbol_it_never_traded() -> None:
    """A fill it somehow hears for an untraded symbol reads honestly absent
    rather than a fabricated flat (ADR-0041 §3)."""
    _, _, strategy, _ = _harness()
    filled = OrderFilled(
        ts_event=2,
        ts_init=2,
        cloid="0xabc",
        strategy_id="trivial",
        signal_id="trivial:BTC:1",
        symbol="BTC",
        trade_id="v1",
        quantity=Decimal("0.5"),
        price=Decimal("42000"),
        cum_qty=Decimal("0.5"),
    )

    asyncio.run(_deliver(strategy, [filled]))

    assert strategy.positions == [None]


async def _deliver(strategy: SingleShotMarketStrategy, events: list) -> None:
    for event in events:
        await strategy.on_order_event(event)


async def _record(sink: list, signal: object) -> None:
    sink.append(signal)


def test_set_next_seq_resumes_the_recovered_counter() -> None:
    bus, _, strategy, signals = _harness()
    strategy.set_next_seq(7)

    asyncio.run(bus.publish(_tick()))

    assert [s.signal_id for s in signals] == ["trivial:BTC:7"]


def test_snapshot_round_trip_preserves_the_single_shot_state() -> None:
    bus, _, fired, _ = _harness()
    asyncio.run(bus.publish(_tick()))

    _, _, restored, signals = _harness()
    restored.restore(fired.snapshot())
    asyncio.run(bus.publish(_tick(ts=2_000)))

    # The restored strategy knows it already fired: single shot means one ever.
    assert signals == []


def test_restore_rejects_unreadable_bytes() -> None:
    _, _, strategy, _ = _harness()

    with pytest.raises(ValueError):
        strategy.restore(b"\xff not a snapshot")
