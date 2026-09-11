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
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

from tickwright.adapters.feed import ReplayFeedConfig
from tickwright.adapters.paper import PaperExchangeConfig
from tickwright.adapters.paper.funding import HOUR_NS
from tickwright.adapters.store import SQLiteStoreConfig
from tickwright.app.build import build_engine
from tickwright.app.config import AppConfig, StrategyConfig
from tickwright.domain import InstrumentSpec, LeverageSpec, Portfolio, Side

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


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        replay=ReplayFeedConfig(path=_write_ticks(tmp_path / "ticks.jsonl")),
        sqlite=SQLiteStoreConfig(path=tmp_path / "ledger.db"),
        paper=PaperExchangeConfig(instrument_specs={"BTC": SPEC}, genesis_collateral=GENESIS),
        leverage={"BTC": LEVERAGE},
        strategies=[
            StrategyConfig(
                kind="single_shot_market",
                strategy_id="demo",
                symbol="BTC",
                side=Side.BUY,
                quantity=QUANTITY,
            )
        ],
    )


async def _until(condition: Callable[[], bool]) -> None:
    async def poll() -> None:
        while not condition():
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), timeout=5)


def _run(config: AppConfig) -> Portfolio:
    """One graceful life: build, run until the mark lands, stop."""
    engine = build_engine(config)
    portfolio = engine.portfolio_for("demo")

    def marked() -> bool:
        view = portfolio.position("BTC")
        return view is not None and view.funding != 0 and view.unrealized_pnl is not None

    async def life() -> int:
        run = asyncio.create_task(engine.run())
        await _until(marked)
        await engine.stop()
        return await run

    assert asyncio.run(life()) == 0
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
