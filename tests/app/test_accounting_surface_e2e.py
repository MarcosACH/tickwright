"""The accounting surface end to end, wired from ``AppConfig`` (issue #259).

Every sibling of PRD #168 asserts its own layer. This suite asserts the
assembled path a real run takes: ``ReplayFeed`` -> ``PaperExchange`` ->
``PortfolioProjection`` -> store, built by ``build_engine`` from a pure
``AppConfig``. No venue, no network, no ambient ``TICKWRIGHT_*``.

Expected values are hand-computed from the scenario's literals, never read back
from the code. The arithmetic is spelled out beside each constant.
"""

import asyncio
import json
import os
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


def _funded(funding: Decimal) -> Callable[[Portfolio], bool]:
    """The book has settled ``funding`` and holds a mark: the scenario has landed."""

    def settled(portfolio: Portfolio) -> bool:
        view = portfolio.position("BTC")
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


def _refused(config: AppConfig) -> str:
    """Run ``config`` and return the fault that stopped it before the feed started.

    ``engine.run`` never raises. It faults, returns non-zero for the supervisor,
    and names the cause once on the trail. The trail is the only place the
    reason is legible, so that is what a refusal test reads.
    """
    engine = build_engine(config)
    with capture_events() as logs:
        exit_code = asyncio.run(engine.run())

    assert exit_code != 0
    assert engine.state is ComponentState.FAULTED
    assert [log for log in logs if log["event"] == "engine.feed_started"] == []
    faults = [log for log in logs if log["event"] == "engine.faulted"]
    assert len(faults) == 1
    return str(faults[0]["error"])


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

    assert "StoreAccountMismatch" in error
    assert "genesis_collateral" in error
    assert str(GENESIS) in error
    assert str(GENESIS * 2) in error


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

    assert "StoreAccountMismatch" in error
    assert "account_id" in error
    assert "paper-default" in error
    assert "paper-other" in error
