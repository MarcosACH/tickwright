"""All four pre-trade caps together, as an operator runs them (issue #383).

Each cap slice of PRD #377 proves its own cap. This suite proves the four at
once on the assembled engine: ``ReplayFeed`` -> scripted strategy ->
``ExecutionManager`` -> ``RealGuard`` -> ``PaperExchange``. The limits come
from a pure ``AppConfig``, and the concretes come from the public ``app``
builders. No venue, no network, no ambient ``TICKWRIGHT_*``.

The engine is wired here, not by ``build_engine``, for two reasons. The
scenario needs a strategy that sends a script of orders, which no configured
kind does. And the Kafka run needs the fake broker in place of a real client.

Expected outcomes are read off the caps and the script by hand. The arithmetic
is spelled out beside each step.
"""

import asyncio
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
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
from tickwright.app.config import AppConfig, StrategyConfig
from tickwright.domain import (
    Clock,
    EventBus,
    InstrumentSpec,
    MarketTick,
    OrderDenied,
    OrderEvent,
    OrderFilled,
    OrderType,
    Portfolio,
    Side,
    TimeInForce,
)
from tickwright.engine.guard import PreTradeLimits, RateCap, SymbolLimits
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
    taker_fee=Decimal("0.00045"),
    funding_rate=Decimal("0"),
    max_leverage=50,
    margin_maint=Decimal("0.01"),
)

LIMITS = PreTradeLimits(
    symbols={
        "BTC": SymbolLimits(
            max_order_size=Decimal("0.5"),
            max_order_value=Decimal("20000"),
            max_position=Decimal("1.3"),
        )
    },
    mark_max_age_seconds=5,
    rate_cap=RateCap(max_orders=2, window_seconds=1),
)

FILLED = "filled"


@dataclass(frozen=True, slots=True)
class Order:
    side: Side
    quantity: Decimal
    symbol: str = "BTC"


def buy(quantity: str) -> Order:
    return Order(Side.BUY, Decimal(quantity))


# One BTC trade tick every two seconds, so each tick opens a fresh rate cap
# window. The orders in a tick are sent in list order at that tick's time.
SCRIPT: list[tuple[int, list[Order]]] = [
    # 0.6 > 0.5 size cap. 0.45 * 50000 = 22500 > 20000 value cap. 0.3 fills.
    (0, [buy("0.6"), buy("0.45"), buy("0.3")]),
    # A burst of three. Two fill, the third finds the window full. Position 0.5.
    (2, [buy("0.1"), buy("0.1"), buy("0.1")]),
    # A position-building run: 0.85, then 1.2, then 1.55 > 1.3 is denied.
    (4, [buy("0.35")]),
    (6, [buy("0.35")]),
    (8, [buy("0.35")]),
]

EXPECTED = [
    "above max order size 0.5",
    "above max order value 20000",
    FILLED,
    FILLED,
    FILLED,
    "above max orders per window 2 in 1s",
    FILLED,
    FILLED,
    "above max position 1.3",
]


class ScriptedStrategy:
    """Sends the orders a script lists for each tick it sees, at market.

    ``outcomes`` records, in send order, ``"filled"`` or the denial reason."""

    def __init__(
        self,
        *,
        strategy_id: str,
        bus: EventBus,
        clock: Clock,
        script: Sequence[Sequence[Order]],
    ) -> None:
        self.strategy_id = strategy_id
        self._emitter = SignalEmitter(strategy_id=strategy_id, bus=bus, clock=clock)
        self._script = script
        self._ticks_seen = 0
        self._sent: list[str] = []
        self._results: dict[str, str] = {}

    @property
    def outcomes(self) -> list[str | None]:
        return [self._results.get(signal_id) for signal_id in self._sent]

    async def on_tick(self, tick: MarketTick) -> None:
        index = self._ticks_seen
        self._ticks_seen += 1
        if index >= len(self._script):
            return
        for order in self._script[index]:
            signal_id = await self._emitter.place(
                symbol=order.symbol,
                side=order.side,
                quantity=order.quantity,
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.IOC,
            )
            self._sent.append(signal_id)

    async def on_order_event(self, event: OrderEvent) -> None:
        if isinstance(event, OrderFilled):
            self._results[event.signal_id] = FILLED
        elif isinstance(event, OrderDenied):
            self._results[event.signal_id] = event.reason

    def set_next_seq(self, next_seq: int) -> None:
        self._emitter.set_next_seq(next_seq)

    def snapshot(self) -> bytes:
        return json.dumps({"version": 1, "ticks_seen": self._ticks_seen}).encode()

    def restore(self, data: bytes) -> None:
        state = json.loads(data)
        if state.get("version") != 1:
            raise ValueError(f"unknown snapshot version: {state.get('version')!r}")
        self._ticks_seen = int(state["ticks_seen"])


def _write_ticks(path: Path, script: list[tuple[int, list[Order]]]) -> Path:
    rows = [
        {
            "symbol": "BTC",
            "price": str(PRICE),
            "size": "10",
            "aggressor_side": "buy",
            "trade_id": f"t{i}",
            "ts_event": seconds * SECOND_NS,
        }
        for i, (seconds, _) in enumerate(script)
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        replay=ReplayFeedConfig(path=_write_ticks(tmp_path / "ticks.jsonl", SCRIPT)),
        sqlite=SQLiteStoreConfig(path=tmp_path / "ledger.db"),
        paper=PaperExchangeConfig(instrument_specs={"BTC": SPEC}, genesis_collateral=GENESIS),
        limits=LIMITS,
        # Declares what this process trades, which the limits must name. The
        # engine below runs the scripted strategy under this id instead.
        strategies=[
            StrategyConfig(
                kind="single_shot_market",
                strategy_id="scripted",
                symbol="BTC",
                side=Side.BUY,
                quantity=Decimal("0.1"),
            )
        ],
    )


@dataclass
class _Life:
    engine: Engine
    strategy: ScriptedStrategy
    portfolio: Portfolio


def _wire(config: AppConfig, *, bus: EventBus | None = None) -> _Life:
    """``build_engine`` with the scripted strategy registered and the bus swappable."""
    bus = bus if bus is not None else build_bus(config)
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
        strategy_id="scripted", bus=bus, clock=clock, script=[orders for _, orders in SCRIPT]
    )
    engine.register(strategy, symbols={"BTC"})
    return _Life(engine=engine, strategy=strategy, portfolio=engine.portfolio_for("scripted"))


async def _until(condition: Callable[[], bool]) -> None:
    async def poll() -> None:
        while not condition():
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), timeout=5)


def _run(life: _Life, expected: Sequence[object]) -> None:
    """Run until every expected order has an outcome, then stop gracefully."""

    def settled() -> bool:
        outcomes = life.strategy.outcomes
        return len(outcomes) == len(expected) and None not in outcomes

    async def go() -> int:
        run = asyncio.create_task(life.engine.run())
        await _until(settled)
        await life.engine.stop()
        return await run

    assert asyncio.run(go()) == 0


def _position(portfolio: Portfolio) -> Decimal:
    view = portfolio.position("BTC")
    return Decimal(0) if view is None else view.size


def test_each_cap_denies_by_name_and_orders_inside_every_cap_fill(tmp_path: Path) -> None:
    life = _wire(_config(tmp_path))
    _run(life, EXPECTED)

    assert life.strategy.outcomes == EXPECTED
    # 0.3 + 0.1 + 0.1 + 0.35 + 0.35
    assert _position(life.portfolio) == Decimal("1.2")
