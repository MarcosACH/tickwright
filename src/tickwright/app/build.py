"""The composition root (ADR-0032): one explicit builder, ``match`` selection.

The one module at the top of the graph that knows every concrete. Each seam
has a public per-seam builder with an explicit ``match`` over its config
discriminant; ``build_engine`` composes them and hands the ``Engine``
already-built dependencies. Adding an impl is one ``Literal`` value in
``config.py`` and one ``match`` arm here — no registry, no import-path DSL.
"""

from collections.abc import Mapping
from typing import assert_never

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.feed import ReplayFeed
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    Clock,
    EventBus,
    Exchange,
    InstrumentSpec,
    MarketFeed,
    PreTradeGuard,
    ReplayClock,
    Store,
    Strategy,
)
from tickwright.engine.guard import NoopGuard, RealGuard
from tickwright.engine.runner import Engine
from tickwright.strategies import SingleShotLimitStrategy, SingleShotMarketStrategy

from .config import AppConfig, StrategyConfig


def build_bus(config: AppConfig) -> EventBus:
    match config.bus:
        case "in_memory":
            return InMemoryBus()
        case "kafka":
            # Imported here, not at module top: selecting the hermetic default
            # must not load the wire stack (serde/aiokafka) at all.
            from tickwright.adapters.bus.kafka import KafkaBus

            return KafkaBus(
                bootstrap_servers=config.kafka.bootstrap_servers,
                topic=config.kafka.events_topic,
                group_id=config.kafka.group_id,
            )
        case unreachable:
            assert_never(unreachable)


def build_store(config: AppConfig) -> Store:
    match config.store:
        case "sqlite":
            return SQLiteStore(config.sqlite.path)
        case "postgres":
            # Imported here, not at module top: selecting the hermetic default
            # must not load the psycopg driver at all.
            from tickwright.adapters.store.postgres import PostgresStore

            return PostgresStore(config.postgres.dsn)
        case unreachable:
            assert_never(unreachable)


def build_exchange(config: AppConfig, *, bus: EventBus, clock: Clock) -> Exchange:
    match config.exchange:
        case "paper":
            # The paper venue subscribes itself to the tick stream (it fills off
            # ticks, a real venue would not) — no tick-wiring line to keep here.
            return PaperExchange(
                bus=bus,
                clock=clock,
                fill_model=ImmediateFillModel(),
                instrument_specs=config.paper.instrument_specs,
            )
        case unreachable:
            assert_never(unreachable)


def build_feed(config: AppConfig, *, bus: EventBus, clock: ReplayClock) -> MarketFeed:
    match config.feed:
        case "replay":
            return ReplayFeed(path=config.replay.path, bus=bus, clock=clock)
        case unreachable:
            assert_never(unreachable)


def build_guard(
    config: AppConfig,
    *,
    specs: Mapping[str, InstrumentSpec],
    store: Store,
    clock: Clock,
) -> PreTradeGuard:
    match config.guard:
        case "real":
            return RealGuard(specs=specs, store=store, clock=clock)
        case "noop":
            return NoopGuard()
        case unreachable:
            assert_never(unreachable)


def build_engine(config: AppConfig) -> Engine:
    """Construct every concrete and hand the ``Engine`` a wired, tradable stack.

    The venue's ``InstrumentSpec``s flow into the venue-agnostic guard here
    (ADR-0031) — the one placement where both sides are concrete.
    """
    bus = build_bus(config)
    # The replay feed drives virtual time (ADR-0027); the live slice will
    # derive a LiveClock from a live feed discriminant when one ships.
    clock = ManualClock()
    store = build_store(config)
    exchange = build_exchange(config, bus=bus, clock=clock)
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
    )
    for strategy_config in config.strategies:
        engine.register(
            _build_strategy(strategy_config, bus=bus, clock=clock),
            symbols={strategy_config.symbol},
        )
    return engine


def _build_strategy(config: StrategyConfig, *, bus: EventBus, clock: Clock) -> Strategy:
    match config.kind:
        case "single_shot_market":
            return SingleShotMarketStrategy(
                strategy_id=config.strategy_id,
                bus=bus,
                clock=clock,
                side=config.side,
                quantity=config.quantity,
            )
        case "single_shot_limit":
            assert config.price is not None  # enforced by StrategyConfig validation
            return SingleShotLimitStrategy(
                strategy_id=config.strategy_id,
                bus=bus,
                clock=clock,
                side=config.side,
                quantity=config.quantity,
                price=config.price,
            )
        case unreachable:
            assert_never(unreachable)
