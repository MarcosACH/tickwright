"""A strategy's order whose type and price do not fit (issue #513).

A LIMIT with no price, or a MARKET with a price. The engine is wired from a
pure ``AppConfig`` with the public ``app`` builders: ``ReplayFeed`` ->
strategy -> ``ExecutionManager`` -> ``PaperExchange``. No venue, no network,
no ambient ``TICKWRIGHT_*``.

The strategy's first order is the bad one. Its second is a valid market buy.
"""

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import pytest
from never_wakes import NeverWakes

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

BTC = InstrumentSpec(
    symbol="BTC",
    sz_decimals=3,
    max_decimals=6,
    min_notional=Decimal("10"),
    max_leverage=50,
    margin_maint=Decimal("0.01"),
)


class SendsABadOrderOnce(NeverWakes):
    """Sends the bad order on its first tick, then a market buy."""

    def __init__(
        self, emitter: SignalEmitter, *, order_type: OrderType, price: Decimal | None
    ) -> None:
        self.strategy_id = emitter.strategy_id
        self._emitter = emitter
        self._order_type = order_type
        self._price = price
        self.errors: list[Exception] = []
        self.sent: list[str] = []
        self.states: dict[str, list[OrderState]] = {}

    async def on_tick(self, tick: MarketTick) -> None:
        if not self.errors:
            try:
                await self._emitter.place(
                    symbol="BTC",
                    side=Side.BUY,
                    quantity=Decimal("1"),
                    order_type=self._order_type,
                    time_in_force=TimeInForce.GTC,
                    price=self._price,
                )
            except ValueError as error:
                self.errors.append(error)
                # Raise on, so the strategy host has to contain it.
                raise
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

    def set_next_seq(self, next_seq: int) -> None:
        self._emitter.set_next_seq(next_seq)

    def snapshot(self) -> bytes:
        return json.dumps({"version": 1}).encode()

    def restore(self, data: bytes) -> None:
        pass


@pytest.mark.parametrize(
    ("order_type", "price"),
    [(OrderType.LIMIT, None), (OrderType.MARKET, Decimal("100"))],
    ids=["limit-with-no-price", "market-with-a-price"],
)
def test_a_bad_order_raises_in_the_strategy_and_the_engine_keeps_trading(
    tmp_path: Path, order_type: OrderType, price: Decimal | None
) -> None:
    ticks = tmp_path / "ticks.jsonl"
    ticks.write_text(
        "".join(
            json.dumps(
                {
                    "symbol": "BTC",
                    "price": "50000",
                    "size": "10",
                    "aggressor_side": "buy",
                    "trade_id": f"t{i}",
                    "ts_event": (i + 1) * SECOND_NS,
                }
            )
            + "\n"
            for i in range(2)
        )
    )
    config = AppConfig(
        replay=ReplayFeedConfig(path=ticks),
        sqlite=SQLiteStoreConfig(path=tmp_path / "ledger.db"),
        paper=PaperExchangeConfig(
            instrument_specs={"BTC": BTC},
            genesis_collateral=Decimal("1000000"),
        ),
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
    strategy = SendsABadOrderOnce(engine.emitter_for("buyer"), order_type=order_type, price=price)
    engine.register(strategy, symbols={"BTC"})

    def filled() -> bool:
        return bool(strategy.sent) and OrderState.FILLED in strategy.states.get(
            strategy.sent[0], []
        )

    async def go() -> int | None:
        run = asyncio.create_task(engine.run())

        async def settled() -> None:
            while not filled():
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
    assert len(strategy.errors) == 1
    # The bad order used no seq, so the valid one is seq 1, and it filled.
    assert strategy.sent == ["buyer:BTC:1"]
    assert filled()
