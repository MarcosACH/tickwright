"""The LIMIT + cancel E2E (issue #13): the resting-book and cancel paths driven
through the whole real pipeline.

``ReplayFeed`` tick → ``SingleShotLimitStrategy`` LIMIT ``PlaceSignal`` →
``ExecutionManager`` → ``PaperExchange`` rests it on the book → a later crossing
tick fills it, or a ``CancelSignal`` cancels it → the strategy sees the terminal
``OrderFilled`` / ``OrderCancelled``. Everything real, zero external services,
zero sleeps: ``ManualClock`` + ``InMemoryBus``, driven by a JSONL file. This
wiring doubles as the pipeline's spec until the composition root lands.
"""

import asyncio
import json
from decimal import Decimal
from pathlib import Path

from ledgers import ledger

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.feed import ReplayFeed
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    Event,
    ExecutionReport,
    MarketTick,
    OrderCancelled,
    OrderEvent,
    OrderFilled,
    OrderLive,
    OrderState,
    Side,
    Signal,
    derive_cloid,
)
from tickwright.engine.cache import Cache
from tickwright.engine.execution import ExecutionManager
from tickwright.strategies import SingleShotLimitStrategy

# The paper account's opening cash. The venue requires it (ADR-0042 §1: the
# engine supplies no collateral of its own); these tests do not exercise the
# ledger, so one shared declaration keeps every wiring site honest and quiet.
_GENESIS = Decimal("100000")


def _write_ticks(path: Path, prices: list[str]) -> Path:
    rows = [
        {
            "symbol": "BTC",
            "price": price,
            "size": "3",
            "aggressor_side": "sell",
            "trade_id": chr(ord("a") + i),
            "ts_event": 1_000 * (i + 1),
        }
        for i, price in enumerate(prices)
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def _run(
    path: Path, *, cancel_after_ticks: int | None = None
) -> tuple[list[Event], SingleShotLimitStrategy, SQLiteStore]:
    bus = InMemoryBus()
    clock = ManualClock()
    store = SQLiteStore(":memory:")
    exchange = PaperExchange(
        bus=bus, clock=clock, fill_model=ImmediateFillModel(), genesis_collateral=_GENESIS
    )
    cache = Cache(store=store)
    manager = ExecutionManager(
        bus=bus, clock=clock, exchange=exchange, cache=cache, portfolio=ledger()
    )
    strategy = SingleShotLimitStrategy(
        strategy_id="trivial",
        bus=bus,
        clock=clock,
        side=Side.BUY,
        quantity=Decimal("0.5"),
        price=Decimal("41000"),
        cancel_after_ticks=cancel_after_ticks,
    )
    feed = ReplayFeed(path=path, bus=bus, clock=clock)

    recorded: list[Event] = []
    bus.subscribe(Event, lambda ev: _record(recorded, ev))
    bus.subscribe(MarketTick, strategy.on_tick)
    bus.subscribe(Signal, manager.on_signal)
    bus.subscribe(ExecutionReport, manager.on_execution_report)
    bus.subscribe(OrderEvent, strategy.on_order_event)

    asyncio.run(feed.start())
    return recorded, strategy, store


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
    # Tick 1 at 42000: rests. Tick 2 still at 42000 (no cross): the strategy
    # cancels its own order, and the venue confirms it.
    path = _write_ticks(tmp_path / "ticks.jsonl", ["42000", "42000"])
    _, strategy, store = _run(path, cancel_after_ticks=1)

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
    first, _, _ = _run(path, cancel_after_ticks=1)
    second, _, _ = _run(path, cancel_after_ticks=1)
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
