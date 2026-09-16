"""Runs the paper exchange against the live Hyperliquid feed with
RoundTripLoopStrategy (round_trip_strategy.py) instead of a shipped
StrategyConfig `kind`.

Mirrors src/tickwright/app/__main__.py: same AppSettings, same build_engine
building blocks, same supervised Engine.run() lifecycle (0 = graceful stop,
non-zero = FAULTED). The one difference is the strategy — built here by hand
and engine.register()'d, since the throwaway strategy stays out of AppConfig
on purpose (see round_trip_strategy.py's docstring).

Configure via the normal TICKWRIGHT_* env / .env, with TICKWRIGHT_STRATEGIES
unset or "[]" — this script owns strategy registration, not the config.
Run: uv run python prototypes/live_demo/run_live_paper.py
"""

import asyncio
import sys
from decimal import Decimal

from round_trip_strategy import RoundTripLoopStrategy

from tickwright.app.build import (
    build_bus,
    build_clock,
    build_exchange,
    build_feed,
    build_guard,
    build_store,
    resolve_leverage,
)
from tickwright.app.config import AppSettings
from tickwright.engine.runner import Engine
from tickwright.observability.logging import configure_logging

DEMO_SYMBOL = "BTC"
DEMO_QUANTITY = Decimal("0.005")
DEMO_HOLD_SECONDS = 20.0
DEMO_CYCLE_SECONDS = 15.0


def main() -> int:
    config = AppSettings()
    if config.strategies:
        raise SystemExit("this script registers its own strategy — set TICKWRIGHT_STRATEGIES=[]")
    configure_logging(secrets=config.secrets())

    bus = build_bus(config)
    clock = build_clock(config)
    store = build_store(config)
    leverage = resolve_leverage(config)
    exchange = build_exchange(config, bus=bus, clock=clock, store=store, leverage=leverage)
    feed = build_feed(config, bus=bus, clock=clock)
    guard = build_guard(config, specs=exchange.instrument_specs(), store=store, clock=clock)
    engine = Engine(
        bus=bus,
        clock=clock,
        store=store,
        exchange=exchange,
        feed=feed,
        guard=guard,
        config=config.engine,
        leverage=leverage,
    )

    strategy = RoundTripLoopStrategy(
        strategy_id="round_trip_demo",
        bus=bus,
        clock=clock,
        symbol=DEMO_SYMBOL,
        quantity=DEMO_QUANTITY,
        hold_seconds=DEMO_HOLD_SECONDS,
        cycle_seconds=DEMO_CYCLE_SECONDS,
    )
    engine.register(strategy, symbols={DEMO_SYMBOL})

    return asyncio.run(engine.run())


if __name__ == "__main__":
    sys.exit(main())
