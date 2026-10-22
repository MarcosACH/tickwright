"""A strategy reads its own open orders (ADR-0041 §3), driven through the real engine.

The ``Engine`` with a scripted strategy over ``ReplayFeed``, ``PaperExchange``,
``InMemoryBus``, ``SQLiteStore`` and ``ManualClock``. The strategy acts on one
tick and reads ``Portfolio.open_orders()`` on a later one. No external service.

A second engine life on the same store is a restart. There the strategy also
reads in ``restore()``, which is where it rebuilds after a crash.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from decimal import Decimal
from pathlib import Path

import pytest
from ledgers import GENESIS
from venue_doubles import VenueLink

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.feed import ReplayFeed
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.adapters.store import SQLiteStore
from tickwright.app.build import paper_store_reads
from tickwright.domain import (
    Exchange,
    MarketTick,
    OpenOrdersReadBeforeStart,
    OpenOrderView,
    OrderEvent,
    OrderState,
    OrderType,
    PlaceOrder,
    Portfolio,
    Side,
    SignalEmitter,
    TimeInForce,
)
from tickwright.engine.runner import Engine


class _ScriptedStrategy:
    """Runs one scripted step per tick and keeps every open-orders read."""

    def __init__(
        self, *, emitter: SignalEmitter, portfolio: Portfolio, steps: list["Step"]
    ) -> None:
        self.strategy_id = emitter.strategy_id
        self.emitter = emitter
        self.portfolio = portfolio
        self._steps = list(steps)
        self.reads: list[tuple[OpenOrderView, ...]] = []
        self.restore_reads: list[tuple[OpenOrderView, ...]] = []

    async def on_tick(self, tick: MarketTick) -> None:
        if self._steps:
            _, _, action = self._steps.pop(0)
            await action(self)

    async def on_order_event(self, event: OrderEvent) -> None:
        return None

    def set_next_seq(self, next_seq: int) -> None:
        self.emitter.set_next_seq(next_seq)

    def snapshot(self) -> bytes:
        return b""

    def restore(self, data: bytes) -> None:
        # Where a strategy rebuilds its state after a restart.
        self.restore_reads.append(self.portfolio.open_orders())


class _ReadsInConstructor(_ScriptedStrategy):
    def __init__(
        self, *, emitter: SignalEmitter, portfolio: Portfolio, steps: list["Step"]
    ) -> None:
        super().__init__(emitter=emitter, portfolio=portfolio, steps=steps)
        self.reads.append(portfolio.open_orders())


# One step per tick: the tick's symbol and price, and what the strategy does when it arrives.
Step = tuple[str, str, Callable[[_ScriptedStrategy], Awaitable[None]]]

_ABOVE = "42000"  # far above the resting limit, so it does not fill
_CROSSING = "29000"  # a sell print below the limit, so it fills


def _rest(symbol: str) -> Step:
    """A buy limit at 30000, below the replayed price, so it rests on the book."""

    async def step(strategy: _ScriptedStrategy) -> None:
        await strategy.emitter.place(
            symbol=symbol,
            side=Side.BUY,
            quantity=Decimal("0.1"),
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.GTC,
            price=Decimal("30000"),
        )

    return symbol, _ABOVE, step


def _cancel(symbol: str, target_signal_id: str) -> Step:
    async def step(strategy: _ScriptedStrategy) -> None:
        await strategy.emitter.cancel(symbol=symbol, target_signal_id=target_signal_id)

    return symbol, _ABOVE, step


def _tick(symbol: str, price: str) -> Step:
    """A tick the strategy does nothing on. The venue still matches against it."""

    async def step(strategy: _ScriptedStrategy) -> None:
        return None

    return symbol, price, step


def _read(symbol: str, *, only: str | None = None) -> Step:
    """Read the open orders on a ``symbol`` tick, narrowed to ``only`` when given."""

    async def step(strategy: _ScriptedStrategy) -> None:
        strategy.reads.append(strategy.portfolio.open_orders(only))

    return symbol, _ABOVE, step


def _write_ticks(path: Path, prints: list[tuple[str, str]]) -> Path:
    rows = [
        {
            "symbol": symbol,
            "price": price,
            "size": "3",
            "aggressor_side": "sell",
            "trade_id": f"t{i}",
            "ts_event": 1_000 * (i + 1),
        }
        for i, (symbol, price) in enumerate(prints)
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def _engine(
    tmp_path: Path, ticks: Path, *, link: Callable[[PaperExchange], Exchange] | None = None
) -> tuple[Engine, InMemoryBus, SQLiteStore]:
    """A paper engine over the store in ``tmp_path``, so a second call is the next life.

    Paper reads the store the way ``app/build.py`` wires it, so a later life
    rebuilds the orders an earlier one left resting (#484). ``link`` puts a
    transport in front of the venue.
    """
    bus = InMemoryBus()
    clock = ManualClock()
    store = SQLiteStore(tmp_path / "saga.db")
    reads = paper_store_reads(store)
    paper = PaperExchange(
        bus=bus,
        clock=clock,
        fill_model=ImmediateFillModel(),
        genesis_collateral=GENESIS,
        account_net=reads.account_net,
        applied_fills=reads.applied_fills,
        resting_orders=reads.resting_orders,
    )
    feed = ReplayFeed(path=ticks, bus=bus, clock=clock)
    engine = Engine(
        bus=bus,
        clock=clock,
        store=store,
        exchange=link(paper) if link else paper,
        feed=feed,
    )
    return engine, bus, store


def _run(tmp_path: Path, steps: list[Step]) -> _ScriptedStrategy:
    """One engine life with one strategy: replay one tick per step, then stop gracefully."""
    return _run_strategies(tmp_path, {"scripted": steps})["scripted"]


def _run_strategies(tmp_path: Path, scripts: dict[str, list[Step]]) -> dict[str, _ScriptedStrategy]:
    """One engine life with one strategy per script.

    Two strategies cannot own one symbol (ADR-0034), so each script trades its
    own symbols and a strategy sees only those ticks. The scripts' ticks are
    interleaved, step 1 of each script before step 2 of any. So a read in one
    script comes after the other script's earlier steps.
    """
    prints = [
        (symbol, price)
        for round_ in zip(*scripts.values(), strict=True)
        for symbol, price, _ in round_
    ]
    ticks = _write_ticks(tmp_path / "ticks.jsonl", prints)

    async def main() -> dict[str, _ScriptedStrategy]:
        engine, bus, _ = _engine(tmp_path, ticks)
        strategies = {
            strategy_id: _ScriptedStrategy(
                emitter=engine.emitter_for(strategy_id),
                portfolio=engine.portfolio_for(strategy_id),
                steps=steps,
            )
            for strategy_id, steps in scripts.items()
        }
        for strategy_id, steps in scripts.items():
            symbols = {symbol for symbol, _, _ in steps}
            engine.register(strategies[strategy_id], symbols=symbols)

        done = asyncio.Event()

        async def on_tick(tick: MarketTick) -> None:
            if tick.trade_id == f"t{len(prints) - 1}":
                done.set()

        run = asyncio.create_task(engine.run())
        bus.subscribe(MarketTick, on_tick)
        await asyncio.wait_for(done.wait(), timeout=5)
        await engine.stop()
        assert await run == 0
        return strategies

    return asyncio.run(main())


def test_a_resting_limit_reads_back_with_its_placed_terms(tmp_path: Path) -> None:
    strategy = _run(tmp_path, [_rest("BTC"), _read("BTC")])

    assert strategy.reads == [
        (
            OpenOrderView(
                signal_id="scripted:BTC:1",
                symbol="BTC",
                side=Side.BUY,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.GTC,
                quantity=Decimal("0.1"),
                filled_quantity=Decimal("0"),
                price=Decimal("30000"),
                post_only=False,
                reduce_only=False,
                state=OrderState.LIVE,
                cancel_requested=False,
            ),
        )
    ]


def test_a_filled_order_leaves_the_read(tmp_path: Path) -> None:
    strategy = _run(tmp_path, [_rest("BTC"), _read("BTC"), _tick("BTC", _CROSSING), _read("BTC")])

    resting, after_fill = strategy.reads
    assert [view.signal_id for view in resting] == ["scripted:BTC:1"]
    assert after_fill == ()


def test_a_cancelled_order_leaves_the_read(tmp_path: Path) -> None:
    strategy = _run(
        tmp_path, [_rest("BTC"), _read("BTC"), _cancel("BTC", "scripted:BTC:1"), _read("BTC")]
    )

    resting, after_cancel = strategy.reads
    assert [view.signal_id for view in resting] == ["scripted:BTC:1"]
    assert after_cancel == ()


def test_a_symbol_read_returns_only_that_symbols_orders(tmp_path: Path) -> None:
    strategy = _run(tmp_path, [_rest("BTC"), _rest("ETH"), _read("BTC"), _read("BTC", only="BTC")])

    every_symbol, btc_only = strategy.reads
    assert sorted(view.symbol for view in every_symbol) == ["BTC", "ETH"]
    assert [view.symbol for view in btc_only] == ["BTC"]


def test_each_strategy_reads_only_its_own_orders(tmp_path: Path) -> None:
    strategies = _run_strategies(
        tmp_path,
        {"alpha": [_rest("BTC"), _read("BTC")], "beta": [_rest("ETH"), _read("ETH")]},
    )

    # Both orders rest before either strategy reads.
    assert [view.signal_id for view in strategies["alpha"].reads[0]] == ["alpha:BTC:1"]
    assert [view.signal_id for view in strategies["beta"].reads[0]] == ["beta:ETH:1"]


def test_a_read_from_a_strategy_constructor_raises(tmp_path: Path) -> None:
    # The engine has not rebuilt its Cache yet, so a read here would report no orders.
    engine, _, _ = _engine(tmp_path, _write_ticks(tmp_path / "ticks.jsonl", [("BTC", _ABOVE)]))

    with pytest.raises(OpenOrdersReadBeforeStart):
        _ReadsInConstructor(
            emitter=engine.emitter_for("scripted"),
            portfolio=engine.portfolio_for("scripted"),
            steps=[],
        )


def test_a_read_from_restore_works(tmp_path: Path) -> None:
    # The first life persists a snapshot, so the second life calls restore().
    _run(tmp_path, [_tick("BTC", _ABOVE)])
    strategy = _run(tmp_path, [_tick("BTC", _ABOVE)])

    assert strategy.restore_reads == [()]


class _Killed(BaseException):
    """The process died. A ``BaseException``, so no containment on the way up swallows it."""


class _DiesOnSecondPlace(VenueLink):
    """The venue link for a doomed life. The process dies sending its second
    order, before the venue gets it. A network boundary is where a double may go."""

    def __init__(self, venue: Exchange) -> None:
        super().__init__(venue)
        self._places = 0

    async def place(self, order: PlaceOrder) -> None:
        self._places += 1
        if self._places == 2:
            raise _Killed
        await super().place(order)


def test_restore_after_a_crash_reads_only_the_orders_still_resting(tmp_path: Path) -> None:
    ticks = _write_ticks(tmp_path / "ticks.jsonl", [("BTC", _ABOVE), ("BTC", _ABOVE)])

    async def crashed_life() -> None:
        engine, _, store = _engine(tmp_path, ticks, link=_DiesOnSecondPlace)
        strategy = _ScriptedStrategy(
            emitter=engine.emitter_for("scripted"),
            portfolio=engine.portfolio_for("scripted"),
            steps=[_rest("BTC"), _rest("BTC")],
        )
        engine.register(strategy, symbols={"BTC"})
        # The engine's task group wraps the death in an exception group.
        with pytest.RaisesGroup(_Killed):
            await asyncio.wait_for(engine.run(), timeout=5)
        # The first order rests. The second died in the send window, so its
        # write-ahead row is all that is left of it.
        assert {(order.signal_id, order.state) for order in store.all_orders()} == {
            ("scripted:BTC:1", OrderState.LIVE),
            ("scripted:BTC:2", OrderState.PENDING),
        }
        store.close()

    asyncio.run(crashed_life())

    # Paper rebuilds the resting order. The barrier finds no venue record for
    # the PENDING one and settles it before restore() runs.
    strategy = _run(tmp_path, [_tick("BTC", _ABOVE)])

    assert [[view.signal_id for view in read] for read in strategy.restore_reads] == [
        ["scripted:BTC:1"]
    ]
