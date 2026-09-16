"""Run the engine with one of the verification strategies instead of a shipped ``kind``.

Mirrors ``src/tickwright/app/__main__.py``: the same ``AppSettings`` read from
the scratch ``.env``, the same per-seam builders, the same ``Engine.run()``
exit-code contract. The differences are the hand-registered strategy and the
leverage book, which ``AppConfig`` refuses for a symbol no configured strategy
trades, so it is read here from ``VERIFY_LEVERAGE`` instead.

Parameters arrive as ``VERIFY_*`` variables (``verify start --param KEY=VALUE``):

    SYMBOL       default BTC
    SYMBOL2      default ETH          (dual_symbol only)
    QUANTITY     default 0.5
    HOLD_TICKS   default 2            (round_trip_then_flat)
    TAKE_PROFIT  default 100          (take_profit, in quote currency)
    LEVERAGE     default {}           (JSON, symbol -> {"mode": .., "leverage": ..})

Usage: run_strategy.py {round_trip_then_flat,take_profit,margin_watch,dual_symbol}
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from decimal import Decimal

from strategies import DualSymbolLeg, MarginWatch, RoundTripThenFlat, TakeProfit, Wiring

from tickwright.app.build import (
    build_bus,
    build_clock,
    build_exchange,
    build_feed,
    build_guard,
    build_store,
)
from tickwright.app.config import AppSettings
from tickwright.domain import LeverageBook, LeverageSpec, Side
from tickwright.engine.runner import Engine
from tickwright.observability.logging import configure_logging

NAMES = ("round_trip_then_flat", "take_profit", "margin_watch", "dual_symbol")


def param(name: str, default: str) -> str:
    return os.environ.get(f"VERIFY_{name}", default)


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in NAMES:
        raise SystemExit(f"usage: run_strategy.py {{{','.join(NAMES)}}}")
    name = sys.argv[1]
    symbol = param("SYMBOL", "BTC")
    symbol2 = param("SYMBOL2", "ETH")
    quantity = Decimal(param("QUANTITY", "0.5"))
    traded = (symbol, symbol2) if name == "dual_symbol" else (symbol,)

    config = AppSettings()
    if config.strategies:
        raise SystemExit("this runner registers its own strategy: set TICKWRIGHT_STRATEGIES=[]")
    configure_logging(secrets=config.secrets())

    configured = {
        sym: LeverageSpec(mode=spec["mode"], leverage=int(spec["leverage"]))
        for sym, spec in json.loads(param("LEVERAGE", "{}")).items()
    }
    leverage = LeverageBook.resolve(configured, traded=traded)

    bus = build_bus(config)
    clock = build_clock(config)
    store = build_store(config)
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

    def wiring(strategy_id: str, sym: str) -> Wiring:
        return Wiring(
            strategy_id=strategy_id,
            bus=bus,
            clock=clock,
            portfolio=engine.portfolio_for(strategy_id),
            symbol=sym,
            quantity=quantity,
        )

    match name:
        case "round_trip_then_flat":
            hold = int(param("HOLD_TICKS", "2"))
            engine.register(
                RoundTripThenFlat(wiring("round_trip", symbol), hold_ticks=hold),
                symbols={symbol},
            )
        case "take_profit":
            target = Decimal(param("TAKE_PROFIT", "100"))
            engine.register(
                TakeProfit(wiring("take_profit", symbol), take_profit=target),
                symbols={symbol},
            )
        case "margin_watch":
            engine.register(MarginWatch(wiring("margin_watch", symbol)), symbols={symbol})
        case "dual_symbol":
            engine.register(
                DualSymbolLeg(wiring("leg_long", symbol), side=Side.BUY), symbols={symbol}
            )
            engine.register(
                DualSymbolLeg(wiring("leg_short", symbol2), side=Side.SELL), symbols={symbol2}
            )

    return asyncio.run(engine.run())


if __name__ == "__main__":
    sys.exit(main())
