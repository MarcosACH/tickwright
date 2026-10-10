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
import gc
import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest
from bus_backends import BUS_BACKENDS, make_bus
from store_backends import POSTGRES_DSN_ENV, STORE_BACKEND_PARAMS, resolve_backend

from tickwright.adapters.feed import ReplayFeedConfig
from tickwright.adapters.paper import PaperExchangeConfig
from tickwright.adapters.store import PostgresStoreConfig, SQLiteStoreConfig
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
    OrderState,
    OrderType,
    Portfolio,
    Side,
    TimeInForce,
)
from tickwright.engine.guard import PreTradeLimits, RateCap, SymbolLimits
from tickwright.engine.runner import Engine
from tickwright.strategies.emitter import SignalEmitter

SECOND_NS = 1_000_000_000
PRICES = {"BTC": Decimal("50000"), "ETH": Decimal("3000")}
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
    rate_cap=RateCap(max_orders=2, window_seconds=1.0),
)

FILLED = "filled"


@dataclass(frozen=True, slots=True)
class Order:
    side: Side
    quantity: Decimal
    symbol: str = "BTC"


def buy(quantity: str) -> Order:
    return Order(Side.BUY, Decimal(quantity))


def sell(quantity: str) -> Order:
    return Order(Side.SELL, Decimal(quantity))


@dataclass(frozen=True, slots=True)
class Step:
    """One trade tick on ``symbol`` at ``seconds``, the orders sent on it in
    list order, and the outcome each order must get."""

    seconds: float
    orders: list[Order]
    expected: list[str]
    symbol: str = "BTC"

    @property
    def ts_ns(self) -> int:
        return int(self.seconds * SECOND_NS)


# Ticks are two seconds apart, so each tick opens a fresh rate cap window.
CAPS = [
    # 0.6 > 0.5 size cap. 0.45 * 50000 = 22500 > 20000 value cap. 0.3 fills.
    Step(
        0,
        [buy("0.6"), buy("0.45"), buy("0.3")],
        ["above max order size 0.5", "above max order value 20000", FILLED],
    ),
    # A burst of three. Two fill, the third finds the window full. Position 0.5.
    Step(
        2,
        [buy("0.1"), buy("0.1"), buy("0.1")],
        [FILLED, FILLED, "above max orders per window 2 in 1.0s"],
    ),
    # A position-building run: 0.85, then 1.2, then 1.55 > 1.3 is denied.
    Step(4, [buy("0.35")], [FILLED]),
    Step(6, [buy("0.35")], [FILLED]),
    Step(8, [buy("0.35")], ["above max position 1.3"]),
]

# Position 1.2 -> 0.6. The sell of 0.6 is above the 0.5 size cap, and
# 0.6 * 50000 = 30000 is above the 20000 value cap. It only reduces, so it
# fills in one order. The mark is this tick's own trade, so it is fresh.
FRESH_CLOSE = Step(10, [sell("0.6")], [FILLED])

# Both ticks sit inside the 1s window of the close above, which still holds one
# of the two slots. The 0.1 buy takes the other one. Position 0.7. The 0.7 sell
# would close the long, but the window is full, so it is denied. The sell has
# its own tick so the buy's fill has landed: a sell checked beside an unfilled
# buy is a flip, not a close, and the size cap would deny it first.
FULL_WINDOW = [
    Step(10.3, [buy("0.1")], [FILLED]),
    Step(10.6, [sell("0.7")], ["above max orders per window 2 in 1.0s"]),
]

# An ETH tick moves the clock to 20s while the BTC mark stays at 10.6s. That
# mark is 9.4s old, past the 5s limit. A 0.1 buy needs the mark for its value,
# so it is denied. The 0.7 sell closes the long and needs no mark, so it fills.
# It is above the 0.5 size cap, and 0.7 * 50000 = 35000 is above the value cap.
STALE_CLOSE = Step(
    20,
    [buy("0.1"), sell("0.7")],
    ["stale mark for max order value 20000", FILLED],
    symbol="ETH",
)

WHOLE_SCENARIO = [*CAPS, FRESH_CLOSE, *FULL_WINDOW, STALE_CLOSE]


class ScriptedStrategy:
    """Sends, at market, the orders its script lists for a tick's time.

    The script is keyed by time, not by tick count, so a restarted strategy
    picks up where the feed is without needing its snapshot.

    ``outcomes`` records, in send order, ``"filled"`` or the denial reason."""

    def __init__(
        self,
        *,
        strategy_id: str,
        bus: EventBus,
        clock: Clock,
        script: Mapping[int, Sequence[Order]],
    ) -> None:
        self.strategy_id = strategy_id
        self._emitter = SignalEmitter(strategy_id=strategy_id, bus=bus, clock=clock)
        self._script = script
        self._sent: list[str] = []
        self._results: dict[str, str] = {}

    @property
    def outcomes(self) -> list[str | None]:
        return [self._results.get(signal_id) for signal_id in self._sent]

    async def on_tick(self, tick: MarketTick) -> None:
        for order in self._script.get(tick.ts_event, []):
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
        return json.dumps({"version": 1}).encode()

    def restore(self, data: bytes) -> None:
        state = json.loads(data)
        if state.get("version") != 1:
            raise ValueError(f"unknown snapshot version: {state.get('version')!r}")


def _write_ticks(path: Path, steps: Sequence[Step]) -> Path:
    rows = [
        {
            "symbol": step.symbol,
            "price": str(PRICES[step.symbol]),
            "size": "10",
            "aggressor_side": "buy",
            "trade_id": f"{step.symbol}-{step.ts_ns}",
            "ts_event": step.ts_ns,
        }
        for step in steps
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def _config(tmp_path: Path, steps: Sequence[Step], **overrides: object) -> AppConfig:
    """The scenario's config over ``tmp_path``. Overrides poke one field."""
    fields: dict[str, object] = {
        "replay": ReplayFeedConfig(path=_write_ticks(tmp_path / "ticks.jsonl", steps)),
        "sqlite": SQLiteStoreConfig(path=tmp_path / "ledger.db"),
        "paper": PaperExchangeConfig(instrument_specs={"BTC": SPEC}, genesis_collateral=GENESIS),
        "limits": LIMITS,
        # Declares what this process trades, which the limits must name. The
        # engine below runs the scripted strategy under this id instead.
        "strategies": [
            StrategyConfig(
                kind="single_shot_market",
                strategy_id="scripted",
                symbol="BTC",
                side=Side.BUY,
                quantity=Decimal("0.1"),
            )
        ],
    }
    return AppConfig(**{**fields, **overrides})  # type: ignore[arg-type]


@dataclass
class _Life:
    engine: Engine
    strategy: ScriptedStrategy
    portfolio: Portfolio


def _wire(config: AppConfig, steps: Sequence[Step], *, bus: EventBus | None = None) -> _Life:
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
        strategy_id="scripted",
        bus=bus,
        clock=clock,
        script={step.ts_ns: step.orders for step in steps},
    )
    engine.register(strategy, symbols={"BTC", "ETH"})
    return _Life(engine=engine, strategy=strategy, portfolio=engine.portfolio_for("scripted"))


async def _until(condition: Callable[[], bool]) -> None:
    async def poll() -> None:
        while not condition():
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), timeout=5)


def _run(life: _Life, expected: Sequence[str], *, crash: bool = False) -> None:
    """Run until every expected order has an outcome, then stop gracefully.

    ``crash`` cancels the run instead. Nothing is torn down, which is what a
    killed process leaves behind for the next life."""

    def settled() -> bool:
        outcomes = life.strategy.outcomes
        return len(outcomes) == len(expected) and None not in outcomes

    async def go() -> int | None:
        run = asyncio.create_task(life.engine.run())
        await _until(settled)
        if crash:
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
            return None
        await life.engine.stop()
        return await run

    exit_code = asyncio.run(go())
    if not crash:
        assert exit_code == 0


def _scenario(
    tmp_path: Path,
    steps: Sequence[Step],
    *,
    crash: bool = False,
    overrides: Mapping[str, object] | None = None,
    bus: EventBus | None = None,
) -> _Life:
    """Run ``steps`` as one life and check every order got its expected outcome."""
    expected = [outcome for step in steps for outcome in step.expected]
    life = _wire(_config(tmp_path, steps, **(overrides or {})), steps, bus=bus)
    _run(life, expected, crash=crash)
    assert life.strategy.outcomes == expected
    return life


def _store_fields(backend: str, tmp_path: Path) -> dict[str, object]:
    """The ``AppConfig`` fields that select ``backend``. Postgres skips without a server."""
    resolve_backend(backend, tmp_path / "ledger.db")
    if backend == "postgres":
        return {
            "store": "postgres",
            "postgres": PostgresStoreConfig(dsn=os.environ[POSTGRES_DSN_ENV]),
        }
    return {}


def _position(portfolio: Portfolio) -> Decimal:
    view = portfolio.position("BTC")
    return Decimal(0) if view is None else view.size


def test_each_cap_denies_by_name_and_orders_inside_every_cap_fill(tmp_path: Path) -> None:
    life = _scenario(tmp_path, CAPS)

    # 0.3 + 0.1 + 0.1 + 0.35 + 0.35
    assert _position(life.portfolio) == Decimal("1.2")


def test_a_close_bigger_than_the_size_and_value_caps_fills_in_one_order(tmp_path: Path) -> None:
    life = _scenario(tmp_path, [*CAPS, FRESH_CLOSE])

    # 1.2 - 0.6
    assert _position(life.portfolio) == Decimal("0.6")


def test_a_close_takes_a_rate_cap_slot_and_is_denied_when_the_window_is_full(
    tmp_path: Path,
) -> None:
    life = _scenario(tmp_path, [*CAPS, FRESH_CLOSE, *FULL_WINDOW])

    # 0.6 + 0.1. The denied close moved nothing.
    assert _position(life.portfolio) == Decimal("0.7")


def test_a_close_bigger_than_the_size_and_value_caps_fills_with_a_stale_mark(
    tmp_path: Path,
) -> None:
    life = _scenario(tmp_path, WHOLE_SCENARIO)

    # 0.7 - 0.7
    assert _position(life.portfolio) == Decimal("0")


@pytest.mark.parametrize("store", STORE_BACKEND_PARAMS)
@pytest.mark.parametrize("bus", BUS_BACKENDS)
def test_the_whole_scenario_gets_the_same_outcomes_on_every_bus_and_store(
    tmp_path: Path, bus: str, store: str
) -> None:
    """Swapping a backend changes durability, never an outcome (ADR-0023/0019)."""
    life = _scenario(
        tmp_path, WHOLE_SCENARIO, bus=make_bus(bus), overrides=_store_fields(store, tmp_path)
    )

    assert _position(life.portfolio) == Decimal("0")


# The first life builds the long to 1.2 and is killed before the t=8 order.
# The second life sends that order: 1.2 + 0.35 = 1.55 > 1.3, so it is denied
# only if the cap reads the recovered position.
BEFORE_CRASH = CAPS[:4]
AFTER_RESTART = CAPS[4:]


@pytest.mark.parametrize("backend", STORE_BACKEND_PARAMS)
def test_after_a_crash_the_max_position_holds_against_the_recovered_position(
    tmp_path: Path, backend: str
) -> None:
    store_fields = _store_fields(backend, tmp_path)
    _scenario(tmp_path, BEFORE_CRASH, crash=True, overrides=store_fields)
    # A killed process loses its connections and its file locks, which is what
    # frees the store lock for the next life (ADR-0052). Collecting the first
    # life does the same here, through the store's finalizer.
    gc.collect()

    life = _scenario(tmp_path, AFTER_RESTART, overrides=store_fields)

    # 0.3 + 0.1 + 0.1 + 0.35 + 0.35, all from the first life.
    assert _position(life.portfolio) == Decimal("1.2")
    store = build_store(_config(tmp_path, AFTER_RESTART, **store_fields))
    try:
        orders = store.all_orders()
    finally:
        store.close()
    signal_ids = [order.signal_id for order in orders]
    assert len(signal_ids) == len(set(signal_ids))
    # The five fills of the first life, each placed once and none again.
    assert sorted(o.quantity for o in orders if o.state is OrderState.FILLED) == [
        Decimal("0.1"),
        Decimal("0.1"),
        Decimal("0.3"),
        Decimal("0.35"),
        Decimal("0.35"),
    ]
