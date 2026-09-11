"""The accounting surface end to end, wired from ``AppConfig`` (issue #259).

Every sibling of PRD #168 asserts its own layer. This suite asserts the
assembled path a real run takes: ``ReplayFeed`` -> ``PaperExchange`` ->
``PortfolioProjection`` -> store, built by ``build_engine`` from a pure
``AppConfig``. No venue, no network, no ambient ``TICKWRIGHT_*``.

Expected values are hand-computed from the scenario's literals, never read back
from the code. The arithmetic is spelled out beside each constant.
"""

import asyncio
import contextlib
import json
import os
import sqlite3
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest
from store_backends import POSTGRES_DSN_ENV, resolve_backend

from tickwright.adapters.feed import ReplayFeedConfig
from tickwright.adapters.paper import PaperExchangeConfig
from tickwright.adapters.paper.funding import HOUR_NS
from tickwright.adapters.store import PostgresStoreConfig, SQLiteStoreConfig
from tickwright.app.build import build_engine, build_store
from tickwright.app.config import AppConfig, StrategyConfig
from tickwright.domain import (
    ComponentState,
    InstrumentSpec,
    LeverageSpec,
    Portfolio,
    Position,
    Side,
    StoreAccountMismatch,
)
from tickwright.observability.testing import capture_events

# A small account against a 10x position, so the liquidation price is a real
# level rather than the ``None`` a well-collateralised long reports.
GENESIS = Decimal("3000")
QUANTITY = Decimal("0.5")
FILL_PRICE = Decimal("42000")
MARK_PRICE = Decimal("42100")

SPEC = InstrumentSpec(
    symbol="BTC",
    sz_decimals=3,
    max_decimals=6,
    min_notional=Decimal("10"),
    taker_fee=Decimal("0.00045"),
    funding_rate=Decimal("0.0001"),
    max_leverage=50,
    margin_maint=Decimal("0.01"),
)
LEVERAGE = LeverageSpec(mode="cross", leverage=10)

# Row a fills the market shot at 42000. Row b sits past the first hourly
# funding boundary, so replaying it settles one funding epoch at the last
# trade before the boundary (42000) and then marks the book at 42100.
ROWS = [
    {
        "symbol": "BTC",
        "price": str(FILL_PRICE),
        "size": "3",
        "aggressor_side": "buy",
        "trade_id": "a",
        "ts_event": 1_000,
    },
    {
        "symbol": "BTC",
        "price": str(MARK_PRICE),
        "size": "3",
        "aggressor_side": "sell",
        "trade_id": "b",
        "ts_event": HOUR_NS + 1_000,
    },
]

# fee = 0.5 * 42000 * 0.00045
EXPECTED_FEE = Decimal("9.45")
# funding = -(0.5 * 42000 * 0.0001), paid by the long
EXPECTED_FUNDING = Decimal("-2.1")
# cash = 3000 - 9.45 - 2.1
EXPECTED_CASH = Decimal("2988.45")
# unrealized = 0.5 * (42100 - 42000)
EXPECTED_UNREALIZED = Decimal("50")
# equity = 2988.45 + 50
EXPECTED_EQUITY = Decimal("3038.45")
# margin used (cross) = 0.5 * 42100 / 10
EXPECTED_MARGIN_USED = Decimal("2105")
# maintenance = 0.5 * 42100 * 0.01
EXPECTED_MAINTENANCE = Decimal("210.5")
# free margin = 3038.45 - 2105
EXPECTED_FREE_MARGIN = Decimal("933.45")
# effective leverage = 21050 / 3038.45 = 6.92787...
EXPECTED_EFFECTIVE_LEVERAGE = Decimal("6.9279")
# liquidation = 42100 - (3038.45 - 210.5) / 0.5 / (1 - 0.01) = 36386.9696...
EXPECTED_LIQUIDATION = Decimal("36386.97")


def _write_ticks(path: Path) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in ROWS) + "\n")
    return path


def _config(tmp_path: Path, **overrides: object) -> AppConfig:
    """The scenario's config over ``tmp_path``; overrides poke one field."""
    fields: dict[str, object] = {
        "replay": ReplayFeedConfig(path=_write_ticks(tmp_path / "ticks.jsonl")),
        "sqlite": SQLiteStoreConfig(path=tmp_path / "ledger.db"),
        "paper": PaperExchangeConfig(instrument_specs={"BTC": SPEC}, genesis_collateral=GENESIS),
        "leverage": {"BTC": LEVERAGE},
        "strategies": [
            StrategyConfig(
                kind="single_shot_market",
                strategy_id="demo",
                symbol="BTC",
                side=Side.BUY,
                quantity=QUANTITY,
            )
        ],
    }
    return AppConfig(**{**fields, **overrides})  # type: ignore[arg-type]


async def _until(condition: Callable[[], bool]) -> None:
    async def poll() -> None:
        while not condition():
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), timeout=5)


def _funded(funding: Decimal, symbol: str = "BTC") -> Callable[[Portfolio], bool]:
    """The book has settled ``funding`` and holds a mark: the scenario has landed."""

    def settled(portfolio: Portfolio) -> bool:
        view = portfolio.position(symbol)
        return view is not None and view.funding == funding and view.unrealized_pnl is not None

    return settled


_SCENARIO_LANDED = _funded(EXPECTED_FUNDING)


def _run(
    config: AppConfig,
    *,
    settled: Callable[[Portfolio], bool] = _SCENARIO_LANDED,
    crash: bool = False,
) -> Portfolio:
    """One life: build, run until ``settled``, then stop gracefully or crash.

    ``crash`` cancels the run instead of asking it to stop. Nothing is torn
    down, which is what a killed process leaves behind for the next life.
    """
    engine = build_engine(config)
    portfolio = engine.portfolio_for("demo")

    async def life() -> int | None:
        run = asyncio.create_task(engine.run())
        await _until(lambda: settled(portfolio))
        if crash:
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
            return None
        await engine.stop()
        return await run

    exit_code = asyncio.run(life())
    if not crash:
        assert exit_code == 0
    return portfolio


def test_one_run_reports_the_hand_computed_book(tmp_path: Path) -> None:
    portfolio = _run(_config(tmp_path))

    position = portfolio.position("BTC")
    assert position is not None
    assert position.size == QUANTITY
    assert position.entry_price == FILL_PRICE
    assert position.fees == EXPECTED_FEE
    assert position.funding == EXPECTED_FUNDING
    assert position.unrealized_pnl == EXPECTED_UNREALIZED
    assert position.margin_used == EXPECTED_MARGIN_USED
    assert position.maintenance_margin == EXPECTED_MAINTENANCE
    assert position.liquidation_price is not None
    assert position.liquidation_price.quantize(Decimal("0.01")) == EXPECTED_LIQUIDATION

    account = portfolio.account()
    assert account.cash == EXPECTED_CASH
    assert account.equity == EXPECTED_EQUITY
    assert account.total_margin_used == EXPECTED_MARGIN_USED
    assert account.total_maintenance_margin == EXPECTED_MAINTENANCE
    assert account.free_margin == EXPECTED_FREE_MARGIN
    assert account.effective_leverage is not None
    assert account.effective_leverage.quantize(Decimal("0.0001")) == EXPECTED_EFFECTIVE_LEVERAGE


@pytest.mark.postgres
def test_the_postgres_run_reports_the_same_book_as_the_sqlite_run(tmp_path: Path) -> None:
    """ADR-0019's parity promise at PRD grain: swapping the store changes
    durability, never a reported number or a durable row."""
    resolve_backend("postgres", tmp_path / "unused.db")  # skips without a server
    postgres_config = _config(
        tmp_path, store="postgres", postgres=PostgresStoreConfig(dsn=os.environ[POSTGRES_DSN_ENV])
    )
    sqlite_config = _config(tmp_path)

    on_sqlite = _run(sqlite_config)
    on_postgres = _run(postgres_config)

    assert on_postgres.position("BTC") == on_sqlite.position("BTC")
    assert on_postgres.account() == on_sqlite.account()
    assert _durable_rows(postgres_config) == _durable_rows(sqlite_config)


def _durable_rows(config: AppConfig) -> tuple[list[Position], Decimal | None, int | None]:
    """What a fresh open of the configured store reads back: partitions, cash, watermark."""
    store = build_store(config)
    try:
        account = store.load_account()
        return (
            store.all_positions(),
            account.cash if account is not None else None,
            store.funding_mark("BTC"),
        )
    finally:
        store.close()


# Life 2 replays the second row again and one row past the second hourly
# boundary. The first epoch is already on the watermark, so it must be dropped.
# The second settles at the last trade before it (42100).
SECOND_LIFE_ROWS = [
    ROWS[1],
    {
        "symbol": "BTC",
        "price": "42200",
        "size": "3",
        "aggressor_side": "buy",
        "trade_id": "c",
        "ts_event": 2 * HOUR_NS + 1_000,
    },
]
# second epoch = -(0.5 * 42100 * 0.0001) = -2.105
# funding line after both epochs = -2.1 - 2.105
EXPECTED_FUNDING_AFTER_RESTART = Decimal("-4.205")
# cash after both epochs = 2988.45 - 2.105
EXPECTED_CASH_AFTER_RESTART = Decimal("2986.345")


def test_a_killed_run_restarts_onto_the_same_book_without_double_counting(
    tmp_path: Path,
) -> None:
    """ADR-0043 section 6: ledger recovery restores every Tier-1 line and the
    watermark. The barrier then heals nothing for a filled order, and a funding
    epoch already on the watermark is dropped rather than paid twice."""
    _run(_config(tmp_path), crash=True)

    second = tmp_path / "second.jsonl"
    second.write_text("\n".join(json.dumps(r) for r in SECOND_LIFE_ROWS) + "\n")
    # No strategy on the second life: the single shot fires once per process,
    # so the book it holds must come back from the ledger, not from a new fill.
    restarted = _run(
        _config(tmp_path, replay=ReplayFeedConfig(path=second), strategies=[], leverage={}),
        settled=_funded(EXPECTED_FUNDING_AFTER_RESTART),
    )

    position = restarted.position("BTC")
    assert position is not None
    assert position.size == QUANTITY
    assert position.entry_price == FILL_PRICE
    assert position.fees == EXPECTED_FEE
    assert position.funding == EXPECTED_FUNDING_AFTER_RESTART
    assert restarted.account().cash == EXPECTED_CASH_AFTER_RESTART

    store = build_store(_config(tmp_path))
    try:
        assert store.funding_mark("BTC") == 2 * HOUR_NS
        assert [p.signed_size for p in store.all_positions()] == [QUANTITY]
    finally:
        store.close()


def _refused(config: AppConfig) -> StoreAccountMismatch:
    """Run ``config`` and return the fault that stopped it before the feed started.

    ``engine.run`` never raises. It faults and returns non-zero for the
    supervisor. The exception itself stays on ``engine.fault``, so a refusal
    test reads the type and the message there rather than off the trail.
    """
    engine = build_engine(config)
    with capture_events() as logs:
        exit_code = asyncio.run(engine.run())

    assert exit_code != 0
    assert engine.state is ComponentState.FAULTED
    assert [log for log in logs if log["event"] == "engine.feed_started"] == []
    fault = engine.fault
    assert isinstance(fault, StoreAccountMismatch)
    return fault


def test_a_changed_genesis_on_the_second_life_is_refused(tmp_path: Path) -> None:
    """ADR-0043 section 10: a store opened at one genesis may not be traded
    under another. The refusal names the field and both values, so the operator
    can tell a typo from a swapped store."""
    _run(_config(tmp_path))

    error = _refused(
        _config(
            tmp_path,
            paper=PaperExchangeConfig(
                instrument_specs={"BTC": SPEC}, genesis_collateral=GENESIS * 2
            ),
            strategies=[],
            leverage={},
        )
    )

    assert "genesis_collateral" in str(error)
    assert str(GENESIS) in str(error)
    assert str(GENESIS * 2) in str(error)


def test_a_changed_account_label_on_the_second_life_is_refused(tmp_path: Path) -> None:
    """ADR-0042 section 5: the label decides the ``paper-<label>`` account id,
    so relabelling a run points it at a ledger another account opened."""
    _run(_config(tmp_path))

    error = _refused(
        _config(
            tmp_path,
            paper=PaperExchangeConfig(
                instrument_specs={"BTC": SPEC},
                genesis_collateral=GENESIS,
                account_label="other",
            ),
            strategies=[],
            leverage={},
        )
    )

    assert "account_id" in str(error)
    assert "paper-default" in str(error)
    assert "paper-other" in str(error)


def test_a_store_with_orders_but_no_ledger_is_refused(tmp_path: Path) -> None:
    """ADR-0043 section 8: a paper store that predates the ledger holds orders
    whose fees and funding cannot be rebuilt. Seeding a fresh ledger over it
    would report a flat account at full cash, so the run is refused instead."""
    _run(_config(tmp_path))
    # The one way to reach that shape from a finished run is to drop the ledger
    # row by hand. The store has no seam for it, because nothing should do it.
    with contextlib.closing(sqlite3.connect(tmp_path / "ledger.db")) as db, db:
        db.execute("DELETE FROM account")

    error = _refused(_config(tmp_path, strategies=[], leverage={}))

    assert "no ledger" in str(error)
    assert "fresh store" in str(error)


def _marked_past_the_boundary(portfolio: Portfolio) -> bool:
    """The second row has marked the book: the first row's fill, if any, is in."""
    view = portfolio.position("BTC")
    return view is not None and view.unrealized_pnl not in (None, Decimal(0))


def test_a_refused_boot_leaves_the_strategy_snapshot_as_it_found_it(tmp_path: Path) -> None:
    """The refusal's remedy is "restore the declared values to resume". A boot
    that refuses before ``host.start()`` has restored nothing, so the snapshot it
    would take is the strategy's blank state. Writing that over the previous
    life's snapshot makes the remedy re-fire the single shot on resume: a second
    order the operator never asked for."""
    _run(_config(tmp_path))

    _refused(
        _config(
            tmp_path,
            paper=PaperExchangeConfig(
                instrument_specs={"BTC": SPEC}, genesis_collateral=GENESIS * 2
            ),
        )
    )

    store = build_store(_config(tmp_path))
    try:
        snapshot = store.load_strategy_snapshot("demo")
        assert snapshot is not None
        assert json.loads(snapshot)["fired"] is True
    finally:
        store.close()

    resumed = _run(_config(tmp_path), settled=_marked_past_the_boundary)

    position = resumed.position("BTC")
    assert position is not None
    assert position.size == QUANTITY
    assert position.fees == EXPECTED_FEE
    store = build_store(_config(tmp_path))
    try:
        assert len(store.all_orders()) == 1
    finally:
        store.close()


ETH_SPEC = InstrumentSpec(
    symbol="ETH",
    sz_decimals=2,
    max_decimals=6,
    min_notional=Decimal("10"),
    taker_fee=Decimal("0.00045"),
    funding_rate=Decimal("0.0001"),
    max_leverage=50,
    margin_maint=Decimal("0.01"),
)
ETH_QUANTITY = Decimal("2")
# The ETH rows interleave with the BTC ones: a fill before the boundary and a
# mark past it, so both books settle one epoch and hold a mark.
TWO_SYMBOL_ROWS = [
    ROWS[0],
    {
        "symbol": "ETH",
        "price": "2500",
        "size": "10",
        "aggressor_side": "sell",
        "trade_id": "e1",
        "ts_event": 1_500,
    },
    ROWS[1],
    {
        "symbol": "ETH",
        "price": "2520",
        "size": "10",
        "aggressor_side": "buy",
        "trade_id": "e2",
        "ts_event": HOUR_NS + 1_500,
    },
]
# ETH funding = -(-2 * 2500 * 0.0001), received by the short
EXPECTED_ETH_FUNDING = Decimal("0.5")


def test_two_strategies_partition_the_book_and_sum_to_the_account_net(tmp_path: Path) -> None:
    """ADR-0034 and ADR-0041 section 8: the ledger is partitioned by strategy,
    every fill lands in the partition that placed it, and the per-strategy sizes
    sum to the account net the venue holds. Nothing falls into the reserved
    unattributed partition on the paper path."""
    ticks = tmp_path / "two.jsonl"
    ticks.write_text("\n".join(json.dumps(r) for r in TWO_SYMBOL_ROWS) + "\n")
    config = _config(
        tmp_path,
        replay=ReplayFeedConfig(path=ticks),
        paper=PaperExchangeConfig(
            instrument_specs={"BTC": SPEC, "ETH": ETH_SPEC}, genesis_collateral=GENESIS
        ),
        leverage={"BTC": LEVERAGE, "ETH": LEVERAGE},
        strategies=[
            StrategyConfig(
                kind="single_shot_market",
                strategy_id="demo",
                symbol="BTC",
                side=Side.BUY,
                quantity=QUANTITY,
            ),
            StrategyConfig(
                kind="single_shot_market",
                strategy_id="hedge",
                symbol="ETH",
                side=Side.SELL,
                quantity=ETH_QUANTITY,
            ),
        ],
    )
    engine = build_engine(config)
    demo = engine.portfolio_for("demo")
    hedge = engine.portfolio_for("hedge")

    async def life() -> int:
        run = asyncio.create_task(engine.run())
        eth_landed = _funded(EXPECTED_ETH_FUNDING, "ETH")
        await _until(lambda: _SCENARIO_LANDED(demo) and eth_landed(hedge))
        await engine.stop()
        return await run

    assert asyncio.run(life()) == 0

    assert [p.symbol for p in demo.open_positions()] == ["BTC"]
    assert [p.symbol for p in hedge.open_positions()] == ["ETH"]
    btc = demo.position("BTC")
    eth = hedge.position("ETH")
    assert btc is not None and btc.size == QUANTITY
    assert eth is not None and eth.size == -ETH_QUANTITY
    assert engine.portfolio.account_net() == {"BTC": QUANTITY, "ETH": -ETH_QUANTITY}
    assert engine.portfolio.open_positions(strategy_id=None) == ()

    store = build_store(config)
    try:
        assert sorted((p.strategy_id, p.symbol) for p in store.all_positions()) == [
            ("demo", "BTC"),
            ("hedge", "ETH"),
        ]
    finally:
        store.close()
