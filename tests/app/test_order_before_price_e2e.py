"""A strategy's order sent before its symbol has a price (issue #509).

The engine is wired from a pure ``AppConfig`` with the public ``app``
builders: ``ReplayFeed`` -> strategy -> ``ExecutionManager`` ->
``PaperExchange``. No venue, no network, no ambient ``TICKWRIGHT_*``.

The strategy trades BTC but also hears ETH. The first tick is ETH, so its
first BTC order goes out while paper has no BTC price.
"""

import asyncio
import json
from decimal import Decimal
from pathlib import Path

from tickwright.adapters.feed import ReplayFeedConfig
from tickwright.adapters.paper import PaperExchangeConfig
from tickwright.adapters.store import SQLiteStoreConfig
from tickwright.app.build import (
    build_bus,
    build_clock,
    build_exchange,
    build_feed,
    build_guard,
    build_store,
    resolve_leverage,
)
from tickwright.app.config import AppConfig
from tickwright.domain import (
    InstrumentSpec,
    MarketTick,
    OrderEvent,
    OrderState,
    OrderType,
    Side,
    SignalEmitter,
    TimeInForce,
)
from tickwright.engine.runner import Engine

SECOND_NS = 1_000_000_000


def spec(symbol: str) -> InstrumentSpec:
    return InstrumentSpec(
        symbol=symbol,
        sz_decimals=3,
        max_decimals=6,
        min_notional=Decimal("10"),
        max_leverage=50,
        margin_maint=Decimal("0.01"),
    )


class BuysBtcOnEveryTick:
    """Sends one BTC market buy per tick it hears, on any symbol."""

    def __init__(self, emitter: SignalEmitter) -> None:
        self.strategy_id = emitter.strategy_id
        self._emitter = emitter
        self.sent: list[str] = []
        self.states: dict[str, list[OrderState]] = {}

    async def on_tick(self, tick: MarketTick) -> None:
        signal_id = await self._emitter.place(
            symbol="BTC",
            side=Side.BUY,
            quantity=Decimal("1"),
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.IOC,
        )
        self.sent.append(signal_id)

    async def on_order_event(self, event: OrderEvent) -> None:
        self.states.setdefault(event.signal_id, []).append(event.state)

    def final(self, index: int) -> OrderState | None:
        states = self.states.get(self.sent[index], [])
        return states[-1] if states else None

    def set_next_seq(self, next_seq: int) -> None:
        self._emitter.set_next_seq(next_seq)

    def snapshot(self) -> bytes:
        return json.dumps({"version": 1}).encode()

    def restore(self, data: bytes) -> None:
        pass


def test_an_order_before_the_first_price_fails_and_the_engine_keeps_trading(
    tmp_path: Path,
) -> None:
    ticks = tmp_path / "ticks.jsonl"
    ticks.write_text(
        "".join(
            json.dumps(
                {
                    "symbol": symbol,
                    "price": price,
                    "size": "10",
                    "aggressor_side": "buy",
                    "trade_id": f"t{i}",
                    "ts_event": (i + 1) * SECOND_NS,
                }
            )
            + "\n"
            for i, (symbol, price) in enumerate([("ETH", "3000"), ("BTC", "50000")])
        )
    )
    config = AppConfig(
        replay=ReplayFeedConfig(path=ticks),
        sqlite=SQLiteStoreConfig(path=tmp_path / "ledger.db"),
        paper=PaperExchangeConfig(
            instrument_specs={"BTC": spec("BTC"), "ETH": spec("ETH")},
            genesis_collateral=Decimal("1000000"),
        ),
        # The real guard denies a market order with no mark. This test is about
        # the venue side, so the guard steps aside.
        guard="noop",
    )
    bus = build_bus(config)
    clock = build_clock(config)
    store = build_store(config)
    leverage = resolve_leverage(config)
    exchange = build_exchange(config, bus=bus, clock=clock, store=store, leverage=leverage)
    engine = Engine(
        bus=bus,
        clock=clock,
        store=store,
        exchange=exchange,
        feed=build_feed(config, bus=bus, clock=clock),
        guard=build_guard(config, specs=exchange.instrument_specs(), store=store, clock=clock),
        config=config.engine,
        leverage=leverage,
    )
    strategy = BuysBtcOnEveryTick(engine.emitter_for("buyer"))
    engine.register(strategy, symbols={"BTC", "ETH"})

    async def go() -> int | None:
        run = asyncio.create_task(engine.run())

        async def settled() -> None:
            while len(strategy.sent) < 2 or strategy.final(1) is not OrderState.FILLED:
                await asyncio.sleep(0)

        # An engine that faults ends its run early, so the test reads its exit
        # code instead of timing out.
        waiting = asyncio.create_task(settled())
        await asyncio.wait({run, waiting}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
        waiting.cancel()
        if not run.done():
            await engine.stop()
        return await run

    assert asyncio.run(go()) == 0
    # Paper had no BTC price for the first order, so it never landed. The
    # second came after the BTC tick and filled.
    assert strategy.final(0) is OrderState.FAILED
    assert strategy.final(1) is OrderState.FILLED
