"""The composition root (ADR-0032): one explicit builder, ``match`` selection.

The one module at the top of the graph that knows every concrete. Each seam
has a public per-seam builder with an explicit ``match`` over its config
discriminant; ``build_engine`` composes them and hands the ``Engine``
already-built dependencies. Adding an impl is one ``Literal`` value in
``config.py`` and one ``match`` arm here — no registry, no import-path DSL.
"""

import asyncio
import random
from collections.abc import Mapping
from typing import assert_never

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import LiveClock, ManualClock
from tickwright.adapters.feed import ReplayFeed
from tickwright.adapters.paper import (
    FillModel,
    ImmediateFillModel,
    PaperExchange,
    PaperExchangeConfig,
    StochasticFillModel,
)
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    Clock,
    EventBus,
    Exchange,
    InstrumentSpec,
    MarketFeed,
    Portfolio,
    PreTradeGuard,
    ReplayClock,
    Store,
    Strategy,
    account_net_size,
)
from tickwright.engine.guard import NoopGuard, RealGuard
from tickwright.engine.runner import Engine
from tickwright.strategies import SingleShotLimitStrategy, SingleShotMarketStrategy

# Imported at module top, unlike the kafka/psycopg arms: the venue package is
# light — the websockets/aiohttp/signing stacks load only when a live
# connection or exchange is actually opened.
from tickwright.venues.hyperliquid import (
    HyperliquidExchange,
    HyperliquidFeed,
    fetch_instrument_specs,
)

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


def build_exchange(config: AppConfig, *, bus: EventBus, clock: Clock, store: Store) -> Exchange:
    """Select the venue.

    ``store`` is the paper arm's alone, and it is a **read**: the paper venue is
    in-process, so the only durable answer to "how much of this symbol is held"
    is the ledger's, and its funding generator needs one (ADR-0034's
    Σ-invariant). Wiring it here rather than handing the venue a ledger of its
    own is what keeps that answer single — the live venue is asked nothing,
    because a real one knows.
    """
    match config.exchange:
        case "paper":
            # The paper venue subscribes itself to the tick stream (it fills off
            # ticks, a real venue would not) — no tick-wiring line to keep here.
            # The one place the paper genesis narrows from ``Decimal | None``
            # to ``Decimal``: ``AppConfig``'s validator has already refused a
            # paper run without one, and this arm is the single reader of
            # ``config.paper`` (ADR-0042 §1).
            assert config.paper.genesis_collateral is not None
            return PaperExchange(
                bus=bus,
                clock=clock,
                fill_model=build_fill_model(config.paper, clock=clock),
                genesis_collateral=config.paper.genesis_collateral,
                account_label=config.paper.account_label,
                instrument_specs=config.paper.instrument_specs,
                # Read per funding span, never cached: the mass-read is the
                # ledger's own recovery read (ADR-0043 §9) and this asks it once
                # an hour, so there is nothing to buy by holding a copy and a
                # drift to own if we did.
                account_net=lambda: account_net_size(store.all_positions()),
            )
        case "hyperliquid":
            # The venue authors its own specs from the meta endpoint
            # (ADR-0031). Composition is the one read-once moment (no event
            # loop is running yet), so the fetch runs to completion here and
            # the guard gets the specs through exchange.instrument_specs().
            universe = asyncio.run(fetch_instrument_specs(config.hyperliquid))
            return HyperliquidExchange(
                config=config.hyperliquid,
                bus=bus,
                clock=clock,
                universe=universe,
                # The boot guards run before the barrier and so cannot be
                # barrier steps, but they face the same boot-time venue and
                # must not mint a budget of their own to disagree with it
                # (ADR-0044 §6, ADR-0046 §3) — handed the engine's, from the
                # one place that holds both configs.
                startup_timeout_seconds=config.engine.startup_reconciliation_timeout_seconds,
            )
        case unreachable:
            assert_never(unreachable)


def build_fill_model(config: PaperExchangeConfig, *, clock: Clock) -> FillModel:
    """Select the paper venue's fill behavior (ADR-0012). Determinism is a
    wiring choice: ``immediate`` uses no RNG; ``stochastic`` gets a seeded one
    plus the same ``Clock`` the venue runs on, so its latency shares virtual
    time under replay."""
    match config.fill_model:
        case "immediate":
            return ImmediateFillModel()
        case "stochastic":
            return StochasticFillModel(
                rng=random.Random(config.seed), clock=clock, params=config.stochastic
            )
        case unreachable:
            assert_never(unreachable)


def build_clock(config: AppConfig) -> Clock:
    """The clock is the feed's consequence, not a free choice: replay drives
    virtual time (ADR-0027), a live feed runs on the wall clock (ADR-0005)."""
    match config.feed:
        case "replay":
            return ManualClock()
        case "hyperliquid":
            return LiveClock()
        case unreachable:
            assert_never(unreachable)


def build_feed(config: AppConfig, *, bus: EventBus, clock: Clock) -> MarketFeed:
    match config.feed:
        case "replay":
            # Both narrowings hold by construction: config validation requires
            # the replay section, and build_clock pairs replay with ManualClock.
            assert config.replay is not None
            assert isinstance(clock, ReplayClock)
            return ReplayFeed(path=config.replay.path, bus=bus, clock=clock)
        case "hyperliquid":
            return HyperliquidFeed(config=config.hyperliquid, bus=bus, clock=clock)
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
    (ADR-0031) — the one placement where both sides are concrete. The portfolio
    is the other half of that story and no longer built here: the ledger and the
    order ``Cache`` must write one store for the fill's single transaction to be
    single (ADR-0043 §4), so the ``Engine`` opens it beside the ``Cache`` from
    the ``store`` and ``Exchange`` it already holds. This root keeps the job
    ADR-0041 §7 gives it — a strategy receives its facade as an ``__init__``
    argument, exactly as it receives its ``Clock`` — and now *resolves* that
    facade off the engine rather than off a projection of its own.
    """
    bus = build_bus(config)
    clock = build_clock(config)
    store = build_store(config)
    exchange = build_exchange(config, bus=bus, clock=clock, store=store)
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
            _build_strategy(
                strategy_config,
                bus=bus,
                clock=clock,
                portfolio=engine.portfolio_for(strategy_config.strategy_id),
            ),
            symbols={strategy_config.symbol},
        )
    return engine


def _build_strategy(
    config: StrategyConfig, *, bus: EventBus, clock: Clock, portfolio: Portfolio
) -> Strategy:
    match config.kind:
        case "single_shot_market":
            return SingleShotMarketStrategy(
                strategy_id=config.strategy_id,
                bus=bus,
                clock=clock,
                portfolio=portfolio,
                side=config.side,
                quantity=config.quantity,
            )
        case "single_shot_limit":
            # Deliberately not handed a portfolio: it reads no position state,
            # and a constructor parameter with no reader is not symmetry, it is
            # an unused seam (ADR-0041 §7).
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
