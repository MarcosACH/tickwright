"""The tracer E2E (issue #11): the thinnest complete pipeline, everything real.

``ReplayFeed`` tick → ``SingleShotMarketStrategy`` MARKET ``PlaceSignal`` →
``ExecutionManager`` → ``PaperExchange`` fill at the latest tick → strategy sees
``OrderFilled`` **and reads the position that fill produced** through the
``Portfolio`` seam. Zero external services, zero sleeps: the whole run is on
``ManualClock``, driven by a JSONL file. The canonical event cascade is
asserted, and the full sequence is identical across repeated runs.

Every test runs parametrized over both bus backends (issue #20): the identical
scenario, cascade order, fill price, and durable trail over ``InMemoryBus``
and over ``KafkaBus`` on the fake broker — swapping the backend changes
durability, never behavior (ADR-0023/0028).

This wiring lives in the test because the composition root (``app``) and the
supervised runner are later slices; here it doubles as the pipeline's spec.
"""

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import pytest
from bus_backends import BUS_BACKENDS, make_bus

from tickwright.adapters.clock import ManualClock
from tickwright.adapters.feed import ReplayFeed
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    Account,
    Event,
    ExecutionReport,
    FillReport,
    MarketTick,
    OrderEvent,
    OrderFilled,
    OrderPlaced,
    OrderState,
    OrderSubmitted,
    OrderType,
    PlaceSignal,
    Side,
    Signal,
    TimeInForce,
    derive_cloid,
)
from tickwright.engine.cache import Cache
from tickwright.engine.execution import ExecutionManager
from tickwright.engine.portfolio import PortfolioProjection
from tickwright.strategies import SingleShotMarketStrategy

# The paper account's opening cash. The venue requires it (ADR-0042 §1: the
# engine supplies no collateral of its own); these tests do not exercise the
# ledger, so one shared declaration keeps every wiring site honest and quiet.
_GENESIS = Decimal("100000")

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


def _run(
    path: Path, backend: str, *, close_with: PlaceSignal | None = None
) -> tuple[list[Event], SingleShotMarketStrategy, SQLiteStore]:
    """Wire and drive the whole pipeline once; return every dispatched event.

    ``close_with`` publishes one more signal after end-of-file, which is how the
    round trip is reached: ``SingleShotMarketStrategy`` fires exactly once by
    design, so the closing leg has to come from outside it.
    """
    bus = make_bus(backend)
    clock = ManualClock()
    store = SQLiteStore(":memory:")
    exchange = PaperExchange(
        bus=bus, clock=clock, fill_model=ImmediateFillModel(), genesis_collateral=_GENESIS
    )
    cache = Cache(store=store)
    spec = exchange.account_spec()
    assert spec.genesis_collateral is not None  # the paper venue always declares one
    projection = PortfolioProjection(
        account=Account.open(spec, genesis_collateral=spec.genesis_collateral, ts_ns=0)
    )
    manager = ExecutionManager(
        bus=bus, clock=clock, exchange=exchange, cache=cache, portfolio=projection
    )
    strategy = SingleShotMarketStrategy(
        strategy_id="trivial",
        bus=bus,
        clock=clock,
        side=Side.BUY,
        quantity=Decimal("0.5"),
        portfolio=projection.for_strategy("trivial"),
    )
    feed = ReplayFeed(path=path, bus=bus, clock=clock)

    # Recorder first, so it captures each event in dispatch (cascade) order.
    recorded: list[Event] = []
    bus.subscribe(Event, lambda ev: _record(recorded, ev))
    bus.subscribe(MarketTick, strategy.on_tick)
    bus.subscribe(Signal, manager.on_signal)
    bus.subscribe(ExecutionReport, manager.on_execution_report)
    bus.subscribe(OrderEvent, strategy.on_order_event)

    async def drive() -> None:
        await bus.start()
        await feed.start()
        if close_with is not None:
            await bus.publish(close_with)
        await bus.drain()
        await bus.close()

    asyncio.run(drive())
    return recorded, strategy, store


@pytest.mark.parametrize("backend", BUS_BACKENDS)
def test_tracer_delivers_order_filled_to_the_strategy(tmp_path: Path, backend: str) -> None:
    path = _write_ticks(tmp_path / "ticks.jsonl")
    _, strategy, _ = _run(path, backend)

    assert len(strategy.fills) == 1
    fill = strategy.fills[0]
    assert isinstance(fill, OrderFilled)
    # Filled against the tick that triggered the order (the latest at place time),
    # not the later 42100 tick — proof of the FIFO's order-independence.
    assert fill.price == Decimal("42000")
    assert fill.quantity == Decimal("0.5")
    assert fill.signal_id == "trivial:BTC:1"


@pytest.mark.parametrize("backend", BUS_BACKENDS)
def test_tracer_delivers_the_position_that_fill_produced_to_the_strategy(
    tmp_path: Path, backend: str
) -> None:
    """The economic half of the tracer: the strategy reads its own position back
    through the ``Portfolio`` seam **in the same handler** that saw the fill.

    That read is coherent by construction rather than by timing — the projection
    is the fill's *writer*, applied synchronously on the fill-apply path, so both
    read-models have already moved when the ``OrderFilled`` is published
    (ADR-0035, ADR-0045 §1).
    """
    path = _write_ticks(tmp_path / "ticks.jsonl")
    _, strategy, _ = _run(path, backend)

    assert len(strategy.positions) == 1
    view = strategy.positions[0]
    assert view is not None
    assert view.symbol == "BTC"
    assert view.size == Decimal("0.5")
    assert view.entry_price == Decimal("42000")
    # Opening a leg realizes nothing, and the Tier-1 lines no slice moves yet
    # still read numbers rather than ``None`` (ADR-0041 §6).
    assert view.realized_pnl == Decimal("0")
    assert view.fees == Decimal("0")
    assert view.funding == Decimal("0")


@pytest.mark.parametrize("backend", BUS_BACKENDS)
def test_tracer_reads_realized_pnl_back_through_the_seam_after_a_round_trip(
    tmp_path: Path, backend: str
) -> None:
    """The full economic tracer: a replayed buy, a closing sell, and the strategy
    reading what the round trip earned — through the seam, not the projection."""
    path = _write_ticks(tmp_path / "ticks.jsonl")
    closing = PlaceSignal(
        ts_event=3_000,
        ts_init=3_000,
        strategy_id="trivial",
        symbol="BTC",
        seq=2,
        side=Side.SELL,
        quantity=Decimal("0.5"),
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.IOC,
    )

    _, strategy, _ = _run(path, backend, close_with=closing)

    opened, closed = strategy.positions
    assert opened is not None and closed is not None
    assert opened.size == Decimal("0.5")
    # The venue's latest tick when the closing signal lands is row 2, 42100 —
    # so 0.5 x (42100 - 42000) = 50, worked from the file rather than the code.
    assert closed.size == Decimal("0")
    assert closed.entry_price == Decimal("0")
    assert closed.realized_pnl == Decimal("50")
    # Flat with history is not "never here": the record survives its own close,
    # realized retained (ADR-0041 §3).


@pytest.mark.parametrize("backend", BUS_BACKENDS)
def test_tracer_event_cascade_is_the_canonical_sequence(tmp_path: Path, backend: str) -> None:
    path = _write_ticks(tmp_path / "ticks.jsonl")
    recorded, _, _ = _run(path, backend)

    assert [type(ev) for ev in recorded] == [
        MarketTick,  # tick 1 published by the feed
        PlaceSignal,  # strategy reacts (reentrant -> FIFO)
        OrderPlaced,  # manager records PENDING
        OrderSubmitted,  # manager sends
        FillReport,  # paper exchange fills at the cached tick
        OrderFilled,  # manager's canonical transition; strategy sees it
        MarketTick,  # tick 2: strategy already fired, no new order
    ]


@pytest.mark.parametrize("backend", BUS_BACKENDS)
def test_tracer_checkpoints_the_saga_durably_through_the_store(
    tmp_path: Path, backend: str
) -> None:
    path = _write_ticks(tmp_path / "ticks.jsonl")
    _, _, store = _run(path, backend)

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


@pytest.mark.parametrize("backend", BUS_BACKENDS)
def test_tracer_is_deterministic_across_repeated_runs(tmp_path: Path, backend: str) -> None:
    path = _write_ticks(tmp_path / "ticks.jsonl")

    first, _, _ = _run(path, backend)
    second, _, _ = _run(path, backend)

    # The entire event stream — ids, timestamps, prices — is identical each run.
    assert _fingerprint(first) == _fingerprint(second)


def test_tracer_behaves_identically_over_both_backends(tmp_path: Path) -> None:
    """The parity promise itself (ADR-0028): one scenario, two transports,
    one observable event stream."""
    path = _write_ticks(tmp_path / "ticks.jsonl")

    in_memory, _, _ = _run(path, "in_memory")
    kafka, _, _ = _run(path, "kafka")

    assert _fingerprint(in_memory) == _fingerprint(kafka)


def _fingerprint(events: list[Event]) -> list[tuple[str, str, int]]:
    return [(type(ev).__name__, ev.event_id, ev.ts_event) for ev in events]


async def _record(sink: list, event: object) -> None:
    sink.append(event)
