"""The composition root (issue #19, ADR-0032): explicit ``match`` selection.

``build_engine(config)`` is the one place that knows every concrete. Each
config discriminant — ``bus``, ``store``, ``exchange``, ``feed``, ``guard`` —
selects its impl through an explicit ``match`` in a public per-seam builder;
an unknown value dies in config validation with a readable error, never deep
in wiring. The built engine is a real, tradable paper stack, proven by
running it end-to-end.
"""

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.feed import ReplayFeed, ReplayFeedConfig
from tickwright.adapters.paper import PaperExchange, PaperExchangeConfig
from tickwright.adapters.store import SQLiteStore, SQLiteStoreConfig
from tickwright.app.build import (
    build_bus,
    build_engine,
    build_exchange,
    build_feed,
    build_guard,
    build_store,
)
from tickwright.app.config import AppConfig, StrategyConfig
from tickwright.domain import InstrumentSpec, OrderState, Side, derive_cloid
from tickwright.engine.guard import NoopGuard, RealGuard
from tickwright.engine.runner import Engine

_SPEC = InstrumentSpec(
    symbol="BTC", sz_decimals=3, max_decimals=6, max_sig_figs=5, min_notional=Decimal("10")
)


def _config(tmp_path: Path, **overrides: object) -> AppConfig:
    """A complete valid config over ``tmp_path``; overrides poke one field."""
    (tmp_path / "ticks.jsonl").touch()
    defaults: dict[str, object] = {
        "replay": {"path": tmp_path / "ticks.jsonl"},
        "sqlite": {"path": tmp_path / "saga.db"},
        "paper": {"instrument_specs": {"BTC": _SPEC}},
    }
    return AppConfig(**{**defaults, **overrides})  # type: ignore[arg-type]


def test_bus_discriminant_selects_the_in_memory_bus(tmp_path: Path) -> None:
    assert isinstance(build_bus(_config(tmp_path, bus="in_memory")), InMemoryBus)


def test_store_discriminant_selects_the_sqlite_store(tmp_path: Path) -> None:
    store = build_store(_config(tmp_path, store="sqlite"))
    assert isinstance(store, SQLiteStore)
    store.close()


def test_exchange_discriminant_selects_the_paper_exchange(tmp_path: Path) -> None:
    exchange = build_exchange(
        _config(tmp_path, exchange="paper"), bus=InMemoryBus(), clock=ManualClock()
    )
    assert isinstance(exchange, PaperExchange)
    # The venue owns the specs (ADR-0031): config flowed through to the seam.
    assert exchange.instrument_specs() == {"BTC": _SPEC}


def test_feed_discriminant_selects_the_replay_feed(tmp_path: Path) -> None:
    feed = build_feed(_config(tmp_path, feed="replay"), bus=InMemoryBus(), clock=ManualClock())
    assert isinstance(feed, ReplayFeed)


@pytest.mark.parametrize(("kind", "expected"), [("real", RealGuard), ("noop", NoopGuard)])
def test_guard_discriminant_selects_its_impl(tmp_path: Path, kind: str, expected: type) -> None:
    config = _config(tmp_path, guard=kind)
    store = SQLiteStore(":memory:")
    try:
        guard = build_guard(config, specs={"BTC": _SPEC}, store=store, clock=ManualClock())
        assert isinstance(guard, expected)
    finally:
        store.close()


@pytest.mark.parametrize("field", ["bus", "store", "exchange", "feed", "guard"])
def test_an_unknown_discriminant_value_is_a_readable_config_error(
    tmp_path: Path, field: str
) -> None:
    with pytest.raises(ValidationError, match=field):
        _config(tmp_path, **{field: "not_a_thing"})


def test_build_engine_wires_a_tradable_paper_engine(tmp_path: Path) -> None:
    """The whole point of the root: the built engine trades on replayed ticks.

    Wired entirely from config — real guard included — it runs both configured
    strategy kinds (the market shot fills, the low limit rests LIVE) and stops
    gracefully; the durable trail is in the configured sqlite file.
    """
    ticks = tmp_path / "ticks.jsonl"
    ticks.write_text(
        '{"symbol": "BTC", "price": "42000", "size": "3", '
        '"aggressor_side": "buy", "trade_id": "a", "ts_event": 1000}\n'
    )
    db = tmp_path / "saga.db"
    config = AppConfig(
        replay=ReplayFeedConfig(path=ticks),
        sqlite=SQLiteStoreConfig(path=db),
        paper=PaperExchangeConfig(instrument_specs={"BTC": _SPEC}),
        strategies=[
            StrategyConfig(
                kind="single_shot_market",
                strategy_id="demo",
                symbol="BTC",
                side=Side.BUY,
                quantity=Decimal("0.5"),
            ),
            StrategyConfig(
                kind="single_shot_limit",
                strategy_id="rester",
                symbol="BTC",
                side=Side.BUY,
                quantity=Decimal("0.5"),
                price=Decimal("41000"),
            ),
        ],
    )
    engine = build_engine(config)
    assert isinstance(engine, Engine)

    wanted = {
        derive_cloid("demo:BTC:1"): OrderState.FILLED,
        derive_cloid("rester:BTC:1"): OrderState.LIVE,
    }

    async def run_until_settled() -> int:
        run = asyncio.create_task(engine.run())
        reader = SQLiteStore(db)
        try:

            def settled() -> bool:
                orders = {c: reader.get_order(c) for c in wanted}
                return all(o is not None and o.state is wanted[c] for c, o in orders.items())

            async def until_settled() -> None:
                while not settled():
                    await asyncio.sleep(0)

            await asyncio.wait_for(until_settled(), timeout=5)
        finally:
            reader.close()
        await engine.stop()
        return await run

    assert asyncio.run(run_until_settled()) == 0


def test_a_limit_strategy_config_requires_a_price() -> None:
    with pytest.raises(ValidationError, match="price"):
        StrategyConfig(
            kind="single_shot_limit",
            strategy_id="rester",
            symbol="BTC",
            side=Side.BUY,
            quantity=Decimal("0.5"),
        )
