"""The tracer E2E (issue #11): the thinnest complete pipeline, everything real.

``ReplayFeed`` tick → ``SingleShotMarketStrategy`` MARKET ``PlaceSignal`` →
``ExecutionManager`` → ``PaperExchange`` fill at the latest tick → strategy sees
``OrderFilled``. Zero external services, zero sleeps: the whole run is on
``ManualClock`` + ``InMemoryBus``, driven by a JSONL file. The canonical event
cascade is asserted, and the full sequence is identical across repeated runs.

This wiring lives in the test because the composition root (``app``) and the
supervised runner are later slices; here it doubles as the pipeline's spec.
"""

import asyncio
import json
from decimal import Decimal
from pathlib import Path

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.feed import ReplayFeed
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    Event,
    ExecutionReport,
    FillReport,
    MarketTick,
    OrderEvent,
    OrderFilled,
    OrderPlaced,
    OrderState,
    OrderSubmitted,
    PlaceSignal,
    Side,
    Signal,
    derive_cloid,
)
from tickwright.engine.execution import ExecutionManager
from tickwright.strategies import SingleShotMarketStrategy

_ROWS = [
    {
        "symbol": "BTC",
        "price": "42000",
        "size": "3",
        "aggressor_side": "buy",
        "trade_id": "a",
        "ts_event": 1_000,
    },
    {
        "symbol": "BTC",
        "price": "42100",
        "size": "3",
        "aggressor_side": "sell",
        "trade_id": "b",
        "ts_event": 2_000,
    },
]


def _write_ticks(path: Path) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in _ROWS) + "\n")
    return path


def _run(path: Path) -> tuple[list[Event], SingleShotMarketStrategy, SQLiteStore]:
    """Wire and drive the whole pipeline once; return every dispatched event."""
    bus = InMemoryBus()
    clock = ManualClock()
    store = SQLiteStore(":memory:")
    exchange = PaperExchange(bus=bus, clock=clock, fill_model=ImmediateFillModel())
    manager = ExecutionManager(bus=bus, clock=clock, exchange=exchange, store=store)
    strategy = SingleShotMarketStrategy(
        strategy_id="trivial", bus=bus, clock=clock, side=Side.BUY, quantity=Decimal("0.5")
    )
    feed = ReplayFeed(path=path, bus=bus, clock=clock)

    # Recorder first, so it captures each event in dispatch (cascade) order.
    recorded: list[Event] = []
    bus.subscribe(Event, lambda ev: _record(recorded, ev))
    bus.subscribe(MarketTick, exchange.on_tick)
    bus.subscribe(MarketTick, strategy.on_tick)
    bus.subscribe(Signal, manager.on_signal)
    bus.subscribe(ExecutionReport, manager.on_execution_report)
    bus.subscribe(OrderEvent, strategy.on_order_event)

    asyncio.run(feed.start())
    return recorded, strategy, store


def test_tracer_delivers_order_filled_to_the_strategy(tmp_path: Path) -> None:
    path = _write_ticks(tmp_path / "ticks.jsonl")
    _, strategy, _ = _run(path)

    assert len(strategy.fills) == 1
    fill = strategy.fills[0]
    assert isinstance(fill, OrderFilled)
    # Filled against the tick that triggered the order (the latest at place time),
    # not the later 42100 tick — proof of the FIFO's order-independence.
    assert fill.price == Decimal("42000")
    assert fill.quantity == Decimal("0.5")
    assert fill.signal_id == "trivial:BTC:1"


def test_tracer_event_cascade_is_the_canonical_sequence(tmp_path: Path) -> None:
    path = _write_ticks(tmp_path / "ticks.jsonl")
    recorded, _, _ = _run(path)

    assert [type(ev) for ev in recorded] == [
        MarketTick,  # tick 1 published by the feed
        PlaceSignal,  # strategy reacts (reentrant -> FIFO)
        OrderPlaced,  # manager records PENDING
        OrderSubmitted,  # manager sends
        FillReport,  # paper exchange fills at the cached tick
        OrderFilled,  # manager's canonical transition; strategy sees it
        MarketTick,  # tick 2: strategy already fired, no new order
    ]


def test_tracer_checkpoints_the_saga_durably_through_the_store(tmp_path: Path) -> None:
    path = _write_ticks(tmp_path / "ticks.jsonl")
    _, _, store = _run(path)

    cloid = derive_cloid("trivial:BTC:1")
    record = store.get_order(cloid)
    assert record is not None
    assert record.state is OrderState.FILLED
    assert record.cum_qty == Decimal("0.5")
    # The whole lifecycle left its durable trail (ADR-0008 checkpoint points).
    assert [state for state, _ in store.history(cloid)] == [
        OrderState.PENDING,
        OrderState.SUBMITTED,
        OrderState.FILLED,
    ]


def test_tracer_is_deterministic_across_repeated_runs(tmp_path: Path) -> None:
    path = _write_ticks(tmp_path / "ticks.jsonl")

    first, _, _ = _run(path)
    second, _, _ = _run(path)

    # The entire event stream — ids, timestamps, prices — is identical each run.
    assert _fingerprint(first) == _fingerprint(second)


def _fingerprint(events: list[Event]) -> list[tuple[str, str, int]]:
    return [(type(ev).__name__, ev.event_id, ev.ts_event) for ev in events]


async def _record(sink: list, event: object) -> None:
    sink.append(event)
