"""The LIMIT + cancel E2E (issue #13): the resting-book and cancel paths driven
through the whole real pipeline.

``ReplayFeed`` tick → ``SingleShotLimitStrategy`` LIMIT ``PlaceSignal`` →
``ExecutionManager`` → ``PaperExchange`` rests it on the book → a later crossing
tick fills it, or a wake-up past the deadline sends a ``CancelSignal`` that
cancels it → the strategy sees the terminal
``OrderFilled`` / ``OrderCancelled``. Everything real, zero external services,
zero sleeps: ``ManualClock`` + ``InMemoryBus``, driven by a JSONL file. The
strategy runs inside the real ``Engine``, so its wake-ups come from the
engine's own wake loop and pass the host's wake gate.
"""

import asyncio
import json
from decimal import Decimal
from pathlib import Path

from exit_runs import paper_on

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.feed import ReplayFeed
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    Event,
    InstrumentSpec,
    OrderCancelled,
    OrderFilled,
    OrderLive,
    OrderState,
    Side,
    derive_cloid,
)
from tickwright.engine.runner import Engine
from tickwright.strategies import SingleShotLimitStrategy

_S = 1_000_000_000
_SPECS = {
    "BTC": InstrumentSpec(symbol="BTC", sz_decimals=3, max_decimals=6, min_notional=Decimal("10"))
}


def _write_ticks(path: Path, prices: list[str]) -> Path:
    rows = [
        {
            "symbol": "BTC",
            "price": price,
            "size": "3",
            "aggressor_side": "sell",
            "trade_id": chr(ord("a") + i),
            # One second apart, so a later tick moves the replay clock past a
            # whole-second deadline and the wake loop fires.
            "ts_event": (i + 1) * _S,
        }
        for i, price in enumerate(prices)
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def _run(
    path: Path, *, cancel_after_seconds: int | None = None, db_name: str = "saga.db"
) -> tuple[list[Event], SingleShotLimitStrategy, SQLiteStore]:
    """Run the engine until the strategy hears its order end, then stop it.

    Returns every bus event, the strategy, and a store reopened on the file
    the stopped engine closed. A second run on the same ``db_name`` would
    restore the first run's state, so a fresh run needs a new name.
    """
    bus = InMemoryBus()
    clock = ManualClock()
    db = path.parent / db_name
    store = SQLiteStore(db)
    engine = Engine(
        bus=bus,
        clock=clock,
        store=store,
        exchange=paper_on(store, bus=bus, clock=clock, specs=_SPECS),
        feed=ReplayFeed(path=path, bus=bus, clock=clock),
    )
    strategy = SingleShotLimitStrategy(
        emitter=engine.emitter_for("trivial"),
        portfolio=engine.portfolio_for("trivial"),
        settings=SingleShotLimitStrategy.Settings(
            side=Side.BUY,
            quantity=Decimal("0.5"),
            price=Decimal("41000"),
            cancel_after_seconds=cancel_after_seconds,
        ),
    )
    engine.register(strategy, symbols={"BTC"})
    recorded: list[Event] = []
    bus.subscribe(Event, lambda ev: _record(recorded, ev))

    async def go() -> int:
        run = asyncio.create_task(engine.run())

        async def order_ended() -> None:
            while not (strategy.fills or strategy.cancellations):
                await asyncio.sleep(0)

        # An engine that faults ends its run early, so the test reads its exit
        # code instead of timing out.
        waiting = asyncio.create_task(order_ended())
        await asyncio.wait({run, waiting}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
        waiting.cancel()
        if not run.done():
            await engine.stop()
        return await run

    assert asyncio.run(go()) == 0
    return recorded, strategy, SQLiteStore(db)


def test_resting_limit_fills_when_a_later_tick_crosses(tmp_path: Path) -> None:
    # Tick 1 at 42000: the BUY LIMIT at 41000 rests. Tick 2 at 41000 crosses it.
    path = _write_ticks(tmp_path / "ticks.jsonl", ["42000", "41000"])
    _, strategy, store = _run(path)

    assert len(strategy.fills) == 1
    fill = strategy.fills[0]
    assert isinstance(fill, OrderFilled)
    assert fill.price == Decimal("41000")  # at the limit price
    assert fill.quantity == Decimal("0.5")

    cloid = derive_cloid("trivial:BTC:1")
    record = store.get_order(cloid)
    assert record is not None
    assert record.state is OrderState.FILLED
    # The resting order left LIVE in its durable trail before it filled.
    assert [state for state, _ in store.history(cloid)] == [
        OrderState.PENDING,
        OrderState.SUBMITTED,
        OrderState.LIVE,
        OrderState.FILLED,
    ]


def test_resting_limit_is_cancelled_end_to_end(tmp_path: Path) -> None:
    # Tick 1 at 42000: rests. Tick 2 still at 42000 (no cross). The wake-up
    # past the deadline makes the strategy cancel its own order, and the venue
    # confirms it.
    path = _write_ticks(tmp_path / "ticks.jsonl", ["42000", "42000"])
    _, strategy, store = _run(path, cancel_after_seconds=1)

    assert strategy.fills == []
    assert len(strategy.cancellations) == 1
    assert isinstance(strategy.cancellations[0], OrderCancelled)

    cloid = derive_cloid("trivial:BTC:1")
    record = store.get_order(cloid)
    assert record is not None
    assert record.state is OrderState.CANCELLED
    assert record.cancel_requested is True


def test_limit_cancel_pipeline_is_deterministic_across_runs(tmp_path: Path) -> None:
    path = _write_ticks(tmp_path / "ticks.jsonl", ["42000", "42000"])
    first, _, _ = _run(path, cancel_after_seconds=1, db_name="first.db")
    second, _, _ = _run(path, cancel_after_seconds=1, db_name="second.db")
    assert _fingerprint(first) == _fingerprint(second)


def test_resting_limit_cascade_reaches_live_before_terminal(tmp_path: Path) -> None:
    path = _write_ticks(tmp_path / "ticks.jsonl", ["42000", "41000"])
    recorded, _, _ = _run(path)
    kinds = [type(ev) for ev in recorded]
    # The order works (LIVE) before it fills — the resting-book path, not an
    # immediate fill.
    assert kinds.index(OrderLive) < kinds.index(OrderFilled)


def _fingerprint(events: list[Event]) -> list[tuple[str, str, int]]:
    return [(type(ev).__name__, ev.event_id, ev.ts_event) for ev in events]


async def _record(sink: list, event: object) -> None:
    sink.append(event)
