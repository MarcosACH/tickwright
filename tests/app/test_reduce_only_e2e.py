"""A strategy's reduce-only order on the paper venue (issue #461, ADR-0057).

The engine is wired from a pure ``AppConfig`` with the public ``app``
builders: ``ReplayFeed`` -> scripted strategy -> ``ExecutionManager`` ->
``PaperExchange``. No venue, no network, no ambient ``TICKWRIGHT_*``.

Each scenario opens a position on the first tick and sends the reduce-only
order on a later one, so the opening fill has reached the store first.
Expected outcomes come from the ADR-0057 placement table, worked by hand.
"""

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest

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
    Clock,
    EventBus,
    InstrumentSpec,
    MarketTick,
    OrderEvent,
    OrderFillEvent,
    OrderState,
    OrderStatusReport,
    OrderType,
    Portfolio,
    Side,
    TimeInForce,
)
from tickwright.engine.runner import Engine
from tickwright.strategies.emitter import SignalEmitter

SECOND_NS = 1_000_000_000
PRICE = Decimal("50000")
GENESIS = Decimal("1000000")

SPEC = InstrumentSpec(
    symbol="BTC",
    sz_decimals=3,
    max_decimals=6,
    min_notional=Decimal("10"),
    max_leverage=50,
    margin_maint=Decimal("0.01"),
)

TERMINAL = {
    OrderState.FILLED,
    OrderState.CANCELLED,
    OrderState.REJECTED,
    OrderState.DENIED,
    OrderState.FAILED,
}


@dataclass(frozen=True, slots=True)
class Send:
    side: Side
    quantity: Decimal
    reduce_only: bool = False
    order_type: OrderType = OrderType.MARKET
    time_in_force: TimeInForce = TimeInForce.IOC
    price: Decimal | None = None


def buy(quantity: str) -> Send:
    return Send(Side.BUY, Decimal(quantity))


def reduce_only_sell(quantity: str) -> Send:
    return Send(Side.SELL, Decimal(quantity), reduce_only=True)


class ScriptedStrategy:
    """Sends the orders its script lists for a tick's time, and keeps every
    order event it hears, per order, in arrival order."""

    def __init__(
        self,
        *,
        bus: EventBus,
        clock: Clock,
        script: Mapping[int, Sequence[Send]],
    ) -> None:
        self.strategy_id = "scripted"
        self._emitter = SignalEmitter(strategy_id=self.strategy_id, bus=bus, clock=clock)
        self._script = script
        self.sent: list[str] = []
        self.events: dict[str, list[OrderEvent]] = {}

    async def on_tick(self, tick: MarketTick) -> None:
        for send in self._script.get(tick.ts_event, []):
            signal_id = await self._emitter.place(
                symbol="BTC",
                side=send.side,
                quantity=send.quantity,
                order_type=send.order_type,
                time_in_force=send.time_in_force,
                price=send.price,
                reduce_only=send.reduce_only,
            )
            self.sent.append(signal_id)

    async def on_order_event(self, event: OrderEvent) -> None:
        self.events.setdefault(event.signal_id, []).append(event)

    def states(self, index: int) -> list[OrderState]:
        return [event.state for event in self.events.get(self.sent[index], [])]

    def filled(self, index: int) -> Decimal:
        fills = [e for e in self.events.get(self.sent[index], []) if isinstance(e, OrderFillEvent)]
        return fills[-1].cum_qty if fills else Decimal("0")

    def reason(self, index: int) -> str | None:
        last = self.events[self.sent[index]][-1]
        return getattr(last, "reason", None)

    def all_terminal(self) -> bool:
        return all(
            any(state in TERMINAL for state in self.states(i)) for i in range(len(self.sent))
        )

    def set_next_seq(self, next_seq: int) -> None:
        self._emitter.set_next_seq(next_seq)

    def snapshot(self) -> bytes:
        return json.dumps({"version": 1}).encode()

    def restore(self, data: bytes) -> None:
        pass


@dataclass
class _Life:
    engine: Engine
    strategy: ScriptedStrategy
    portfolio: Portfolio
    # The venue's own status reports, which carry its reason for a cancel.
    reports: list[OrderStatusReport]


def _wire(
    tmp_path: Path,
    script: Mapping[float, Sequence[Send]],
    *,
    prices: Mapping[float, Decimal] | None = None,
) -> _Life:
    """``build_engine`` with the scripted strategy registered in place of a
    configured one. One trade tick per script time, at ``PRICE`` unless
    ``prices`` names another."""
    prices = prices or {}
    ticks = tmp_path / "ticks.jsonl"
    ticks.write_text(
        "".join(
            json.dumps(
                {
                    "symbol": "BTC",
                    "price": str(prices.get(seconds, PRICE)),
                    "size": "10",
                    "aggressor_side": "buy",
                    "trade_id": f"t{i}",
                    "ts_event": int(seconds * SECOND_NS),
                }
            )
            + "\n"
            for i, seconds in enumerate(sorted(script))
        )
    )
    config = AppConfig(
        replay=ReplayFeedConfig(path=ticks),
        sqlite=SQLiteStoreConfig(path=tmp_path / "ledger.db"),
        paper=PaperExchangeConfig(instrument_specs={"BTC": SPEC}, genesis_collateral=GENESIS),
        # The real guard denies most of these orders on the strategy's own
        # position before paper sees them (ADR-0058). This file pins paper's
        # own table, so the guard steps aside. tests/engine/test_guard_e2e.py
        # covers the guard.
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
    strategy = ScriptedStrategy(
        bus=bus,
        clock=clock,
        script={int(seconds * SECOND_NS): sends for seconds, sends in script.items()},
    )
    engine.register(strategy, symbols={"BTC"})
    reports: list[OrderStatusReport] = []

    async def record(report: OrderStatusReport) -> None:
        reports.append(report)

    bus.subscribe(OrderStatusReport, record)
    return _Life(
        engine=engine,
        strategy=strategy,
        portfolio=engine.portfolio_for("scripted"),
        reports=reports,
    )


def _run(life: _Life, settled: Callable[[], bool]) -> None:
    async def go() -> int | None:
        run = asyncio.create_task(life.engine.run())

        async def poll() -> None:
            while not settled():
                await asyncio.sleep(0)

        await asyncio.wait_for(poll(), timeout=5)
        await life.engine.stop()
        return await run

    assert asyncio.run(go()) == 0


def _size(life: _Life) -> Decimal:
    view = life.portfolio.position("BTC")
    return view.size if view is not None else Decimal("0")


@pytest.mark.parametrize(
    ("order_type", "price"),
    [
        (OrderType.MARKET, None),
        # Below the 50000 trade, so the sell crosses on arrival.
        (OrderType.LIMIT, Decimal("49000")),
    ],
    ids=["market", "ioc-limit"],
)
def test_a_reduce_only_sell_smaller_than_the_long_fills_in_full(
    tmp_path: Path, order_type: OrderType, price: Decimal | None
) -> None:
    # Long 3, sell 1 reduce-only. The net covers the order, so it is accepted
    # as sent. 3 - 1 = 2 left.
    sell = Send(
        Side.SELL,
        Decimal("1"),
        reduce_only=True,
        order_type=order_type,
        time_in_force=TimeInForce.IOC,
        price=price,
    )
    life = _wire(tmp_path, {0: [buy("3")], 2: [sell]})

    _run(life, lambda: len(life.strategy.sent) == 2 and life.strategy.all_terminal())

    assert life.strategy.states(1)[-1] is OrderState.FILLED
    assert life.strategy.filled(1) == Decimal("1")
    assert _size(life) == Decimal("2")


def test_a_resting_reduce_only_gtc_smaller_than_the_long_fills_in_full(tmp_path: Path) -> None:
    # Long 3. A GTC sell of 1 at 51000 rests above the 50000 trade at its full
    # size. The 51000 trade at 4s fills it, and no cut part is cancelled.
    sell = Send(
        Side.SELL,
        Decimal("1"),
        reduce_only=True,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        price=Decimal("51000"),
    )
    life = _wire(
        tmp_path,
        {0: [buy("3")], 2: [sell], 4: []},
        prices={4: Decimal("51000")},
    )

    _run(life, lambda: len(life.strategy.sent) == 2 and life.strategy.all_terminal())

    assert life.strategy.states(1)[-2:] == [OrderState.LIVE, OrderState.FILLED]
    assert life.strategy.filled(1) == Decimal("1")
    assert _size(life) == Decimal("2")
    assert [r for r in life.reports if r.status is OrderState.CANCELLED] == []


@pytest.mark.parametrize(
    "send",
    [
        reduce_only_sell("1"),
        # Below the 50000 trade, so a plain one would cross and fill.
        Send(
            Side.SELL,
            Decimal("1"),
            reduce_only=True,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.IOC,
            price=Decimal("49000"),
        ),
        # Above the 50000 trade, so a plain one would rest.
        Send(
            Side.SELL,
            Decimal("1"),
            reduce_only=True,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.GTC,
            price=Decimal("51000"),
        ),
    ],
    ids=["market", "ioc-limit", "gtc"],
)
def test_a_reduce_only_order_against_a_flat_account_is_rejected(tmp_path: Path, send: Send) -> None:
    # Flat, so there is nothing to reduce. A plain sell would open a short.
    life = _wire(tmp_path, {0: [send]})

    _run(life, lambda: len(life.strategy.sent) == 1 and life.strategy.all_terminal())

    assert life.strategy.states(0)[-1] is OrderState.REJECTED
    assert life.strategy.reason(0) == "reduce-only order would increase position"
    assert _size(life) == Decimal("0")


def test_a_reduce_only_order_on_the_side_of_the_net_is_rejected(tmp_path: Path) -> None:
    # Long 3. A reduce-only buy would grow the long, so it is refused and the
    # long stays 3.
    life = _wire(tmp_path, {0: [buy("3")], 2: [Send(Side.BUY, Decimal("1"), reduce_only=True)]})

    _run(life, lambda: len(life.strategy.sent) == 2 and life.strategy.all_terminal())

    assert life.strategy.states(1)[-1] is OrderState.REJECTED
    assert life.strategy.reason(1) == "reduce-only order would increase position"
    assert _size(life) == Decimal("3")


@pytest.mark.parametrize(
    ("order_type", "price"),
    [
        (OrderType.MARKET, None),
        # Below the 50000 trade, so the sell crosses on arrival.
        (OrderType.LIMIT, Decimal("49000")),
    ],
    ids=["market", "ioc-limit"],
)
def test_a_reduce_only_sell_larger_than_the_long_is_shrunk_to_it(
    tmp_path: Path, order_type: OrderType, price: Decimal | None
) -> None:
    # Long 3, sell 5 reduce-only. The venue shrinks the order to the net, so 3
    # fill and the account ends flat. The saga still asks for 5, so the cut 2
    # end CANCELLED (ADR-0057).
    sell = Send(
        Side.SELL,
        Decimal("5"),
        reduce_only=True,
        order_type=order_type,
        time_in_force=TimeInForce.IOC,
        price=price,
    )
    life = _wire(tmp_path, {0: [buy("3")], 2: [sell]})

    _run(life, lambda: len(life.strategy.sent) == 2 and life.strategy.all_terminal())

    assert life.strategy.states(1)[-2:] == [OrderState.PARTIALLY_FILLED, OrderState.CANCELLED]
    assert life.strategy.filled(1) == Decimal("3")
    assert _size(life) == Decimal("0")
    assert [r.reason for r in life.reports if r.status is OrderState.CANCELLED] == [
        "reduce-only shrink"
    ]


def test_a_resting_reduce_only_gtc_is_shrunk_to_the_long(tmp_path: Path) -> None:
    # Long 3. A GTC sell of 5 at 51000 sits above the 50000 trade, so it rests,
    # shrunk to 3. The 51000 trade at 4s fills those 3, and the cut 2 end
    # CANCELLED. The account ends flat, not short 2.
    sell = Send(
        Side.SELL,
        Decimal("5"),
        reduce_only=True,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        price=Decimal("51000"),
    )
    life = _wire(
        tmp_path,
        {0: [buy("3")], 2: [sell], 4: []},
        prices={4: Decimal("51000")},
    )

    _run(life, lambda: len(life.strategy.sent) == 2 and life.strategy.all_terminal())

    assert life.strategy.states(1)[-3:] == [
        OrderState.LIVE,
        OrderState.PARTIALLY_FILLED,
        OrderState.CANCELLED,
    ]
    assert life.strategy.filled(1) == Decimal("3")
    assert _size(life) == Decimal("0")
    assert [r.reason for r in life.reports if r.status is OrderState.CANCELLED] == [
        "reduce-only shrink"
    ]
