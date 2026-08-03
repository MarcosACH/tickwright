"""Funding end to end: paper generates, the ledger applies, a strategy reads it.

The full supervised pipeline on the hermetic path — ``ReplayFeed`` →
``SingleShotMarketStrategy`` → ``PaperExchange`` → ``Engine`` → ``SQLiteStore``
— with the funding generator running inside it. Nothing sleeps: the file's
``ts_event`` column drives virtual time, and a row far enough ahead is what
crosses the boundaries (ADR-0037, ADR-0033).

That is the acceptance criterion this suite exists for. Funding at the venue's
real cadence is an hour of wall clock a test can never wait for, so the whole
model is built on an injected clock; a suite that could not settle a boundary
without waiting would mean the injection had bought nothing.
"""

import asyncio
import json
from decimal import Decimal
from pathlib import Path

from ledgers import GENESIS

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.feed import ReplayFeed
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    ComponentState,
    InstrumentSpec,
    OrderEvent,
    OrderFilled,
    Portfolio,
    Side,
)
from tickwright.engine.runner import Engine
from tickwright.strategies import SingleShotMarketStrategy

_INTERVAL_NS = 1_000
"""The funding interval this suite runs the venue at.

Short and epoch-aligned exactly as the real hour is — the boundary rule is a
multiple of the interval measured from zero, so shrinking it changes only how
much virtual time a row has to cover. Wiring the real hour here would mean
timestamps in the 10^18 range for no gain in what is being asserted.
"""

_SPEC = InstrumentSpec(
    symbol="BTC",
    sz_decimals=3,
    max_decimals=6,
    min_notional=Decimal("0"),
    # 0.01% per boundary against a 0.5 @ 42 000 long is 2.1 USDC a boundary —
    # a number small enough to check by hand and distinct from every other
    # figure the run produces.
    funding_rate=Decimal("0.0001"),
)

_ROWS: list[dict[str, str | int]] = [
    {
        "symbol": "BTC",
        "price": "42000",
        "size": "3",
        "aggressor_side": "buy",
        "trade_id": "a",
        "ts_event": 500,
    },
    # Three boundaries ahead (1 000, 2 000, 3 000), reached in one row: the
    # virtual-time jump ADR-0037 requires to accrue three payments rather than
    # one, and the reason no test here waits on anything.
    {
        "symbol": "BTC",
        "price": "42100",
        "size": "3",
        "aggressor_side": "sell",
        "trade_id": "b",
        "ts_event": 3_500,
    },
]


def _write_ticks(path: Path) -> Path:
    path.write_text("\n".join(json.dumps(row) for row in _ROWS) + "\n")
    return path


async def _run(ticks: Path, db: Path) -> tuple[int, Portfolio]:
    """One supervised life: buy on the first tick, jump the boundaries, stop.

    The scoped facade comes back with the exit code because it is the seam the
    assertions read: it is the engine's own projection, the other end of what the
    run wrote, and it stays readable after the store closes — the durable half is
    asserted separately, by reopening the file.
    """
    bus = InMemoryBus()
    clock = ManualClock()
    store = SQLiteStore(db)
    exchange = PaperExchange(
        bus=bus,
        clock=clock,
        fill_model=ImmediateFillModel(),
        genesis_collateral=GENESIS,
        instrument_specs={"BTC": _SPEC},
        funding_interval_ns=_INTERVAL_NS,
    )
    feed = ReplayFeed(path=ticks, bus=bus, clock=clock)
    engine = Engine(bus=bus, clock=clock, store=store, exchange=exchange, feed=feed)
    strategy = SingleShotMarketStrategy(
        strategy_id="trivial",
        bus=bus,
        clock=clock,
        portfolio=engine.portfolio_for("trivial"),
        side=Side.BUY,
        quantity=Decimal("0.5"),
    )
    engine.register(strategy, symbols={"BTC"})

    filled = asyncio.Event()

    async def on_order_event(event: OrderEvent) -> None:
        if isinstance(event, OrderFilled):
            filled.set()

    bus.subscribe(OrderEvent, on_order_event)

    run = asyncio.create_task(engine.run())
    await asyncio.wait_for(filled.wait(), timeout=5)
    # Replay runs to end-of-file, which is what carries virtual time across the
    # boundaries; the engine keeps running after it, so the stop is ours to ask.
    await asyncio.sleep(0)
    await engine.stop()
    exit_code = await run
    assert engine.state is ComponentState.STOPPED
    return exit_code, engine.portfolio_for("trivial")


def test_a_replayed_time_jump_accrues_every_boundary_onto_the_strategys_own_line(
    tmp_path: Path,
) -> None:
    """The vertical slice, end to end and without sleeping.

    A 0.5 BTC long at 42 000 pays 2.1 a boundary at 0.01%; the second row jumps
    virtual time across three, so the funding line reads −6.3 and cash reads the
    genesis less exactly that. Both are read where a strategy reads them — the
    scoped ``Portfolio`` facade the engine lends it — because that is the seam
    the whole surface exists to serve.

    The price is the one cached when each boundary matured, not the row that
    carried time past it: a matured cadence fires *before* the row at or past
    its deadline is published (ADR-0033), so all three price at 42 000 rather
    than the 42 100 the jumping row brings.
    """
    exit_code, portfolio = asyncio.run(
        _run(_write_ticks(tmp_path / "ticks.jsonl"), tmp_path / "saga.db")
    )

    view = portfolio.position("BTC")
    assert view is not None
    assert view.funding == Decimal("-6.3")  # 3 x -(0.5 x 42000 x 0.0001)
    assert view.entry_price == Decimal("42000")  # funding reaches no basis
    assert view.realized_pnl == Decimal("0")  # nor any PnL
    assert portfolio.account().cash == GENESIS - Decimal("6.3")
    assert exit_code == 0


def test_a_graceful_stop_leaves_the_funding_line_and_its_mark_durable(tmp_path: Path) -> None:
    """What survives the run is what a restart will read back.

    The ledger is the sole authority on the paper path — this venue holds no
    account truth to reconcile against (ADR-0043 §4) — so the durable side is
    where the claim has to land. The mark comes back at the last boundary
    applied, which is what makes a rerun of this same file converge rather than
    accrue a second time.

    Read through a **reopened** store rather than the run's own, which the
    reverse shutdown closed: what is being asserted is what a restart finds on
    disk, and a handle that never closed would prove less than that.
    """
    db = tmp_path / "saga.db"
    asyncio.run(_run(_write_ticks(tmp_path / "ticks.jsonl"), db))
    store = SQLiteStore(db)

    assert [position.funding for position in store.all_positions()] == [Decimal("-6.3")]
    account = store.load_account()
    assert account is not None
    assert account.cash == GENESIS - Decimal("6.3")
    assert store.funding_mark("BTC") == 3_000
