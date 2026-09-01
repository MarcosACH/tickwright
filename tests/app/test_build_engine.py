"""The composition root (issue #19, ADR-0032): explicit ``match`` selection.

``build_engine(config)`` is the one place that knows every concrete. Each
config discriminant — ``bus``, ``store``, ``exchange``, ``feed``, ``guard`` —
selects its impl through an explicit ``match`` in a public per-seam builder;
an unknown value dies in config validation with a readable error, never deep
in wiring. The built engine is a real, tradable paper stack, proven by
running it end-to-end.
"""

import asyncio
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from hyperliquid_fakes import TEST_SIGNING_KEY, FakeExchangeApi
from ledgers import GENESIS
from pydantic import SecretStr, ValidationError

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.bus.kafka import KafkaBus
from tickwright.adapters.clock import LiveClock, ManualClock
from tickwright.adapters.feed import ReplayFeed, ReplayFeedConfig
from tickwright.adapters.paper import PaperExchange, PaperExchangeConfig
from tickwright.adapters.store import SQLiteStore, SQLiteStoreConfig
from tickwright.app.build import (
    build_bus,
    build_clock,
    build_engine,
    build_exchange,
    build_feed,
    build_guard,
    build_store,
    resolve_leverage,
)
from tickwright.app.config import AppConfig, StrategyConfig
from tickwright.domain import (
    AggressorSide,
    FillReport,
    InstrumentSpec,
    LeverageOutOfBounds,
    LeverageSpec,
    MarketTick,
    OrderState,
    OrderType,
    PlaceOrder,
    Side,
    TimeInForce,
    VenueAccountModeUnsupported,
    derive_cloid,
)
from tickwright.engine.guard import NoopGuard, RealGuard
from tickwright.engine.runner import Engine
from tickwright.observability.catalog import NamedEvent
from tickwright.observability.testing import capture_events
from tickwright.venues.hyperliquid import (
    HyperliquidConfig,
    HyperliquidExchange,
    HyperliquidFeed,
)

_SPEC = InstrumentSpec(
    symbol="BTC", sz_decimals=3, max_decimals=6, max_sig_figs=5, min_notional=Decimal("10")
)


def _config(tmp_path: Path, **overrides: object) -> AppConfig:
    """A complete valid config over ``tmp_path``; overrides poke one field."""
    (tmp_path / "ticks.jsonl").touch()
    defaults: dict[str, object] = {
        "replay": {"path": tmp_path / "ticks.jsonl"},
        "sqlite": {"path": tmp_path / "saga.db"},
        "paper": {"instrument_specs": {"BTC": _SPEC}, "genesis_collateral": GENESIS},
    }
    return AppConfig(**{**defaults, **overrides})  # type: ignore[arg-type]


def test_bus_discriminant_selects_the_in_memory_bus(tmp_path: Path) -> None:
    assert isinstance(build_bus(_config(tmp_path, bus="in_memory")), InMemoryBus)


def test_bus_discriminant_selects_the_kafka_bus(tmp_path: Path) -> None:
    # Selection only — no cluster is contacted until the runner starts the bus,
    # so building the Kafka backend needs no running Kafka.
    config = _config(tmp_path, bus="kafka", kafka={"bootstrap_servers": "kafka:9092"})
    assert isinstance(build_bus(config), KafkaBus)


def test_store_discriminant_selects_the_sqlite_store(tmp_path: Path) -> None:
    store = build_store(_config(tmp_path, store="sqlite"))
    assert isinstance(store, SQLiteStore)
    store.close()


def test_store_discriminant_selects_the_postgres_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Selection only — the driver is stubbed at its boundary so no server is
    # contacted, exactly as the kafka arm builds without a cluster. That the DDL
    # runs against a real Postgres is the store contract suite's job.
    import psycopg

    from tickwright.adapters.store.postgres import PostgresStore

    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: MagicMock())
    store = build_store(_config(tmp_path, store="postgres"))
    assert isinstance(store, PostgresStore)
    store.close()


def test_exchange_discriminant_selects_the_paper_exchange(tmp_path: Path) -> None:
    config = _config(tmp_path, exchange="paper")
    exchange = build_exchange(
        config,
        bus=InMemoryBus(),
        clock=ManualClock(),
        store=SQLiteStore(":memory:"),
        leverage=resolve_leverage(config),
    )
    assert isinstance(exchange, PaperExchange)
    # The venue owns the specs (ADR-0031): config flowed through to the seam.
    assert exchange.instrument_specs() == {"BTC": _SPEC}


def test_exchange_discriminant_selects_hyperliquid_with_meta_sourced_specs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The one live read composition makes: the meta endpoint. Faked at the
    # HTTP boundary so the arm is proven with zero network.
    async def fake_post(url: str, payload: dict) -> object:
        assert url == "https://api.hyperliquid-testnet.xyz/info"
        assert payload == {"type": "meta"}
        return {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40}]}

    monkeypatch.setattr("tickwright.venues.hyperliquid.transport.post_json", fake_post)
    config = _config(
        tmp_path,
        exchange="hyperliquid",
        hyperliquid={"signing_key": TEST_SIGNING_KEY, "symbols": ["BTC"], "testnet": True},
    )
    exchange = build_exchange(
        config,
        bus=InMemoryBus(),
        clock=LiveClock(),
        store=SQLiteStore(":memory:"),
        leverage=resolve_leverage(config),
    )

    assert isinstance(exchange, HyperliquidExchange)
    # The venue authored its specs from meta (ADR-0031), ready for the guard.
    spec = exchange.instrument_specs()["BTC"]
    assert (spec.sz_decimals, spec.max_decimals, spec.max_sig_figs) == (5, 6, 5)


def test_the_hyperliquid_arm_hands_the_adapter_the_engine_s_startup_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one obligation ``Exchange.start()`` puts on *this* function.

    The protocol states it outright: an adapter that makes a blocking venue
    call in ``start()`` owns a timeout on it, and "the composition root must
    hand it the budget to size that timeout with, since ``EngineConfig`` does
    not reach the adapter." ADR-0044 §6 is why it must be *that* budget and not
    one of its own — a boot-time venue read bounded separately would be a second
    timeout free to disagree with the first.

    Asserted end-to-end through the built adapter rather than on the keyword,
    because the budget is only observably wired when it is *spent*: a dark venue
    holds ADR-0046 §3's mode gate in its bounded retry until the window is gone.

    The window is deliberately **not** ``EngineConfig``'s 60 s default. Every
    other construction site in the suite passes 60.0 literally, so at the
    default a hard-coded budget here would be indistinguishable from a wired
    one — the regression this test exists to catch would pass it.
    """
    post = FakeExchangeApi(
        {
            "meta": {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40}]},
            "userAbstraction": ConnectionError("venue dark"),
        }
    )
    monkeypatch.setattr("tickwright.venues.hyperliquid.transport.post_json", post)
    clock = ManualClock(start_ns=0)
    config = _config(
        tmp_path,
        exchange="hyperliquid",
        hyperliquid={"signing_key": TEST_SIGNING_KEY, "symbols": ["BTC"], "testnet": True},
        engine={"startup_reconciliation_timeout_seconds": 300.0},
    )
    exchange = build_exchange(
        config,
        bus=InMemoryBus(),
        clock=clock,
        store=SQLiteStore(":memory:"),
        leverage=resolve_leverage(config),
    )

    with pytest.raises(VenueAccountModeUnsupported):
        asyncio.run(exchange.start())

    asked = [query["type"] for (_url, query) in post.requests]
    assert asked[0] == "meta", "the universe read composition makes before building the adapter"
    assert set(asked[1:]) == {"userAbstraction"}, "then the boot gate, through the built adapter"
    # Spent the configured window, not the 60 s default — within one capped
    # backoff interval of it, which is the overshoot the cap bounds.
    elapsed_seconds = clock.timestamp_ns() / 1_000_000_000
    assert 300.0 <= elapsed_seconds < 330.0


def test_the_hyperliquid_exchange_requires_a_signing_key(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="TICKWRIGHT_HYPERLIQUID__SIGNING_KEY"):
        _config(tmp_path, exchange="hyperliquid", hyperliquid={"symbols": ["BTC"]})


def test_feed_discriminant_selects_the_replay_feed(tmp_path: Path) -> None:
    feed = build_feed(_config(tmp_path, feed="replay"), bus=InMemoryBus(), clock=ManualClock())
    assert isinstance(feed, ReplayFeed)


def test_feed_discriminant_selects_the_hyperliquid_feed(tmp_path: Path) -> None:
    config = _config(tmp_path, feed="hyperliquid", hyperliquid={"symbols": ["BTC"]})
    feed = build_feed(config, bus=InMemoryBus(), clock=LiveClock())
    assert isinstance(feed, HyperliquidFeed)


def test_the_clock_follows_the_feed_discriminant(tmp_path: Path) -> None:
    # Replay drives virtual time (ADR-0027); the live feed runs on the wall
    # clock — the pairing is the root's job, not the operator's.
    assert isinstance(build_clock(_config(tmp_path)), ManualClock)
    live = _config(tmp_path, feed="hyperliquid", hyperliquid={"symbols": ["BTC"]})
    assert isinstance(build_clock(live), LiveClock)


def test_a_hyperliquid_feed_with_no_symbols_is_a_readable_config_error(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="symbol"):
        _config(tmp_path, feed="hyperliquid")


@pytest.mark.parametrize(("kind", "expected"), [("real", RealGuard), ("noop", NoopGuard)])
def test_guard_discriminant_selects_its_impl(tmp_path: Path, kind: str, expected: type) -> None:
    config = _config(tmp_path, guard=kind)
    store = SQLiteStore(":memory:")
    try:
        guard = build_guard(config, specs={"BTC": _SPEC}, store=store, clock=ManualClock())
        assert isinstance(guard, expected)
    finally:
        store.close()


def test_the_ledger_opens_against_the_account_the_built_exchange_declares(
    tmp_path: Path,
) -> None:
    """The run's one ledger is seeded from the venue's own ``AccountSpec``, so
    the configured genesis reaches the cash line a strategy reads (ADR-0042 §6).

    The assertion is on the *configured* number rather than a literal: this is
    what catches a root that wires a ledger against some other account's
    declaration, which no other test here would notice. Reached through the
    engine because the root no longer builds the projection (#213) — the engine
    opens it beside its ``Cache``, and the root only asks for the facade.
    """
    engine = build_engine(_config(tmp_path))

    assert engine.portfolio_for("demo").account().cash == GENESIS


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
    # One symbol each: ADR-0034's disjointness rule forbids two strategies over
    # one symbol on a NET venue, so the two kinds are exercised side by side
    # rather than stacked.
    ticks.write_text(
        '{"symbol": "BTC", "price": "42000", "size": "3", '
        '"aggressor_side": "buy", "trade_id": "a", "ts_event": 1000}\n'
        '{"symbol": "ETH", "price": "42000", "size": "3", '
        '"aggressor_side": "buy", "trade_id": "b", "ts_event": 1001}\n'
    )
    db = tmp_path / "saga.db"
    config = AppConfig(
        replay=ReplayFeedConfig(path=ticks),
        sqlite=SQLiteStoreConfig(path=db),
        paper=PaperExchangeConfig(
            instrument_specs={"BTC": _SPEC, "ETH": _SPEC}, genesis_collateral=GENESIS
        ),
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
                symbol="ETH",
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
        derive_cloid("rester:ETH:1"): OrderState.LIVE,
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


async def _record_fill(sink: list, report: object) -> None:
    sink.append(report)


def _market_tick(price: str) -> MarketTick:
    return MarketTick(
        ts_event=1_000,
        ts_init=1_000,
        symbol="BTC",
        price=Decimal(price),
        size=Decimal("10"),
        aggressor_side=AggressorSide.BUY,
        trade_id="t1",
        seq=0,
    )


def _btc_market_order() -> PlaceOrder:
    return PlaceOrder(
        cloid="0xabc",
        symbol="BTC",
        side=Side.BUY,
        quantity=Decimal("0.5"),
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.IOC,
    )


def _fill_price_from_built_paper(config: AppConfig) -> Decimal:
    bus = InMemoryBus()
    exchange = build_exchange(
        config,
        bus=bus,
        clock=ManualClock(start_ns=1_000),
        store=SQLiteStore(":memory:"),
        leverage=resolve_leverage(config),
    )
    fills: list[FillReport] = []
    bus.subscribe(FillReport, lambda r: _record_fill(fills, r))

    async def scenario() -> None:
        await bus.publish(_market_tick("42000"))
        await exchange.place(_btc_market_order())

    asyncio.run(scenario())
    return fills[0].price


def test_paper_fill_model_defaults_to_immediate_full_fill_zero_slippage(tmp_path: Path) -> None:
    # Default unchanged (ADR-0012): the reproducible ImmediateFillModel fills at
    # the exact tick price, so the built paper stack needs no seed or knobs.
    assert _fill_price_from_built_paper(_config(tmp_path)) == Decimal("42000")


def test_paper_fill_model_selects_the_seeded_stochastic_model_from_config(tmp_path: Path) -> None:
    # The second impl is a wiring choice: config selects it and the seed + knobs
    # flow to the seam, observable as an adverse slip off the tick price.
    config = _config(
        tmp_path,
        paper={
            "instrument_specs": {"BTC": _SPEC},
            "genesis_collateral": GENESIS,
            "fill_model": "stochastic",
            "seed": 7,
            "stochastic": {"prob_slippage": 1.0, "max_slippage": "0.001"},
        },
    )
    price = _fill_price_from_built_paper(config)
    assert Decimal("42000") < price <= Decimal("42000") * (Decimal(1) + Decimal("0.001"))


def _strategy(symbol: str, strategy_id: str = "demo") -> StrategyConfig:
    return StrategyConfig(
        kind="single_shot_market",
        strategy_id=strategy_id,
        symbol=symbol,
        side=Side.BUY,
        quantity=Decimal("0.5"),
    )


def test_a_traded_symbol_absent_from_config_resolves_to_the_safe_default(
    tmp_path: Path,
) -> None:
    """``AppConfig.leverage`` is sparse; what the consumers receive is complete.

    The scope is every symbol the configured strategies declare (ADR-0044 §3),
    and the strategy-traded symbol nobody configured is settled by which way the
    two failures point: *not* covering it leaves the model computing at ``1x``
    while the venue may sit at 20x cross, which understates real risk — the
    dangerous direction. Covering it with the safest pair makes any post-boot
    disagreement real drift rather than a config gap.

    ETH is configured and BTC is not, so this fails if the resolution either
    drops the unconfigured symbol or overwrites the configured one. What the
    completion *means* is ``LeverageBook``'s and pinned there; what this pins is
    the input only the composition root holds — that the set completed over is
    the **strategy-declared** one, not the feed's subscription list and not the
    venue's instrument universe.
    """
    config = _config(
        tmp_path,
        strategies=[_strategy("BTC", "btc"), _strategy("ETH", "eth")],
        leverage={"ETH": LeverageSpec(mode="cross", leverage=10)},
    )

    resolved = resolve_leverage(config)

    assert resolved.entries == {
        "BTC": LeverageSpec(mode="isolated", leverage=1),
        "ETH": LeverageSpec(mode="cross", leverage=10),
    }


def test_the_model_and_the_venue_receive_the_identical_resolved_map(tmp_path: Path) -> None:
    """One resolution, injected twice — the whole point of resolving at the root.

    Asserted from a **single** ``build_engine``, because the claim is about one
    map reaching two consumers rather than about two consumers each computing
    something defensible. The model consumer is read directly; the venue
    consumer is read through the only thing it does with the value in this
    slice — refusing a boot the ``40x`` cap cannot carry (ADR-0044 §9) — which
    is what proves the venue got the same ``50x`` the model holds rather than a
    default it filled in for itself.
    """
    config = _config(
        tmp_path,
        strategies=[_strategy("BTC")],
        leverage={"BTC": LeverageSpec(mode="cross", leverage=50)},
        paper={
            "instrument_specs": {"BTC": replace(_SPEC, max_leverage=40)},
            "genesis_collateral": GENESIS,
        },
    )
    engine = build_engine(config)

    assert engine.portfolio.leverage_for("BTC") == LeverageSpec(mode="cross", leverage=50)

    with capture_events() as events:
        exit_code = asyncio.run(engine.run())

    assert exit_code != 0
    faults = [event for event in events if event["event"] == NamedEvent.ENGINE_FAULTED]
    assert len(faults) == 1
    assert "LeverageOutOfBounds" in faults[0]["error"]
    assert "BTC" in faults[0]["error"]


def test_a_live_run_reads_the_leverage_without_touching_a_paper_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole reason ``leverage`` is a top-level field (ADR-0044 §2).

    ADR-0040 §5 originally put the block in ``PaperExchangeConfig``, which
    cannot stand for a reason independent of any venue write: the consumer is
    the venue-agnostic margin model, needed on both paths, and no live run may
    read ``config.paper`` — the hazard ADR-0042 §1 already litigated for genesis
    collateral, where a ``TICKWRIGHT_PAPER__*`` variable governing a live run is
    the failure.

    This config authors no paper block at all, and the live adapter still
    refuses on the configured ``50x`` against the venue's published ``40x``
    cap — which it could only have read from the venue-agnostic field.
    """
    post = FakeExchangeApi(
        {
            "meta": {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40}]},
            "userAbstraction": "disabled",
        }
    )
    monkeypatch.setattr("tickwright.venues.hyperliquid.transport.post_json", post)
    config = AppConfig(
        exchange="hyperliquid",
        feed="hyperliquid",
        hyperliquid=HyperliquidConfig(
            signing_key=SecretStr(TEST_SIGNING_KEY), symbols=["BTC"], testnet=True
        ),
        strategies=[_strategy("BTC")],
        leverage={"BTC": LeverageSpec(mode="cross", leverage=50)},
    )

    assert config.paper.genesis_collateral is None  # nothing paper was authored

    exchange = build_exchange(
        config,
        bus=InMemoryBus(),
        clock=LiveClock(),
        store=SQLiteStore(":memory:"),
        leverage=resolve_leverage(config),
    )

    with pytest.raises(LeverageOutOfBounds) as refusal:
        asyncio.run(exchange.start())

    assert "BTC" in str(refusal.value)
