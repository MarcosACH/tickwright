"""Multi-strategy E2E (issue #17): two strategies on two symbols in one engine.

The whole real pipeline — ``ReplayFeed`` → ``StrategyHost`` (two reference
``SingleShotMarketStrategy`` instances) → ``ExecutionManager`` →
``PaperExchange`` — proving the ADR-0018 runtime: per-strategy tick routing and
``OrderEvent`` isolation, independent seqs (both strategies consume seq 1 with
no collision), and per-strategy snapshots. A second life over the surviving
store then shows restored strategies stay quiet: the snapshot remembers the
shot was fired, and nothing places twice.
"""

import asyncio
import json
from decimal import Decimal
from pathlib import Path

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.feed import ReplayFeed
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    ExecutionReport,
    MarketTick,
    OrderState,
    Side,
    Signal,
    derive_cloid,
)
from tickwright.engine.cache import Cache
from tickwright.engine.execution import ExecutionManager
from tickwright.engine.strategy_host import StrategyHost
from tickwright.strategies import SingleShotMarketStrategy

_BTC_CLOID = derive_cloid("btc-shot:BTC:1")
_ETH_CLOID = derive_cloid("eth-shot:ETH:1")


def _ticks_file(path: Path, rows: list[tuple[str, str, int]]) -> Path:
    path.write_text(
        "\n".join(
            json.dumps(
                {
                    "symbol": symbol,
                    "price": price,
                    "size": "5",
                    "aggressor_side": "sell",
                    "trade_id": f"{symbol}-{ts}",
                    "ts_event": ts,
                }
            )
            for symbol, price, ts in rows
        )
        + "\n"
    )
    return path


def _life(
    store: SQLiteStore, clock: ManualClock, ticks: Path
) -> tuple[StrategyHost, SingleShotMarketStrategy, SingleShotMarketStrategy, list[Signal]]:
    """One engine life over ``store``: full wiring, feed run to end-of-file."""
    bus = InMemoryBus()
    venue = PaperExchange(bus=bus, clock=clock, fill_model=ImmediateFillModel())
    cache = Cache(store=store)
    cache.rebuild()
    manager = ExecutionManager(bus=bus, clock=clock, exchange=venue, cache=cache)
    host = StrategyHost(bus=bus, clock=clock, store=store)
    btc_strat = SingleShotMarketStrategy(
        strategy_id="btc-shot", bus=bus, clock=clock, side=Side.BUY, quantity=Decimal("1")
    )
    eth_strat = SingleShotMarketStrategy(
        strategy_id="eth-shot", bus=bus, clock=clock, side=Side.SELL, quantity=Decimal("2")
    )
    host.register(btc_strat, symbols={"BTC"})
    host.register(eth_strat, symbols={"ETH"})

    bus.subscribe(MarketTick, venue.on_tick)
    bus.subscribe(Signal, manager.on_signal)
    bus.subscribe(ExecutionReport, manager.on_execution_report)
    host.start()

    signals: list[Signal] = []

    async def record(signal: Signal) -> None:
        signals.append(signal)

    bus.subscribe(Signal, record)

    feed = ReplayFeed(path=ticks, bus=bus, clock=clock)
    asyncio.run(feed.start())
    return host, btc_strat, eth_strat, signals


def test_two_strategies_two_symbols_stay_isolated_across_a_restart(tmp_path: Path) -> None:
    store = SQLiteStore(":memory:")
    clock = ManualClock()
    first_ticks = _ticks_file(
        tmp_path / "first.jsonl",
        [("BTC", "40000", 1_000), ("ETH", "2500", 2_000), ("BTC", "40100", 3_000)],
    )

    host, btc_strat, eth_strat, _ = _life(store, clock, first_ticks)

    # Both strategies consumed *their own* seq 1 — isolated counters, no
    # dedup collision — and each order filled at its own venue price.
    assert [f.cloid for f in btc_strat.fills] == [_BTC_CLOID]
    assert [f.cloid for f in eth_strat.fills] == [_ETH_CLOID]
    # OrderEvent isolation: every event a strategy saw is its own.
    assert all(f.strategy_id == "btc-shot" for f in btc_strat.fills)
    assert all(f.strategy_id == "eth-shot" for f in eth_strat.fills)
    for cloid in (_BTC_CLOID, _ETH_CLOID):
        order = store.get_order(cloid)
        assert order is not None and order.state is OrderState.FILLED

    # Graceful stop: one final snapshot per strategy, independently keyed.
    host.stop()
    btc_snap = store.load_strategy_snapshot("btc-shot")
    eth_snap = store.load_strategy_snapshot("eth-shot")
    assert btc_snap is not None and eth_snap is not None

    # Second life over the surviving store: restored strategies know they
    # already fired — fresh ticks place nothing, and the saga set is unchanged.
    second_ticks = _ticks_file(
        tmp_path / "second.jsonl",
        [("BTC", "40200", 4_000), ("ETH", "2600", 5_000)],
    )
    _, btc_second, eth_second, second_signals = _life(store, clock, second_ticks)

    assert second_signals == []
    assert btc_second.fills == [] and eth_second.fills == []
    assert {order.cloid for order in store.all_orders()} == {_BTC_CLOID, _ETH_CLOID}
