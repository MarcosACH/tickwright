"""A flatten order's fills split over every partition, on the real paper pipeline (ADR-0054).

The order is owned by ``__operator__``, which never owns a partition. Each fill moves the
strategy's partition and the unattributed one toward zero, so both read flat at a full fill.
"""

import asyncio
import random
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest
from ledgers import GENESIS

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.paper import PaperExchange, StochasticFillModel, StochasticParams
from tickwright.adapters.store import SQLiteStore
from tickwright.app.build import paper_store_reads
from tickwright.domain import (
    OPERATOR,
    AccountSpec,
    AggressorSide,
    ExecutionReport,
    InstrumentSpec,
    MarketTick,
    OrderDenied,
    OrderEvent,
    OrderState,
    PlaceSignal,
    ReconciliationFill,
    Side,
    Signal,
    TimeInForce,
    derive_cloid,
)
from tickwright.domain.enums import OrderType
from tickwright.engine.checkpoint import Checkpointer
from tickwright.engine.execution import ExecutionManager
from tickwright.engine.guard import RealGuard
from tickwright.engine.reconcile import ReconcileConfig, Reconciler

_SPEC = InstrumentSpec(
    symbol="BTC", sz_decimals=3, max_decimals=6, max_sig_figs=5, min_notional=Decimal("10")
)


@dataclass(frozen=True, slots=True)
class _Engine:
    bus: InMemoryBus
    store: SQLiteStore
    checks: Checkpointer


def _engine() -> _Engine:
    """A paper engine whose crossing limits fill half their size per tick."""
    bus = InMemoryBus()
    clock = ManualClock(start_ns=1_000)
    store = SQLiteStore(":memory:")
    params = StochasticParams(partial_fill_fraction=Decimal("0.5"))
    reads = paper_store_reads(store)
    exchange = PaperExchange(
        bus=bus,
        clock=clock,
        fill_model=StochasticFillModel(rng=random.Random(0), clock=clock, params=params),
        genesis_collateral=GENESIS,
        account_net=reads.account_net,
        applied_fills=reads.applied_fills,
    )
    checks = Checkpointer(
        spec=AccountSpec(account_id="paper-default", genesis_collateral=GENESIS),
        store=store,
        clock=clock,
        specs={"BTC": _SPEC},
    )
    checks.recover()
    manager = ExecutionManager(
        bus=bus,
        exchange=exchange,
        checkpointer=checks,
        guard=RealGuard(specs={"BTC": _SPEC}, store=store, clock=clock),
    )
    bus.subscribe(Signal, manager.on_signal)
    bus.subscribe(ExecutionReport, manager.on_execution_report)
    return _Engine(bus, store, checks)


def _tick(price: str) -> MarketTick:
    return MarketTick(
        ts_event=1_000,
        ts_init=1_000,
        symbol="BTC",
        price=Decimal(price),
        size=Decimal("1"),
        aggressor_side=AggressorSide.BUY,
        trade_id=f"t-{price}",
        seq=0,
    )


def _crossing_limit(
    strategy_id: str, *, side: Side, quantity: str, reduce_only: bool = False
) -> PlaceSignal:
    # Priced through the market, so each tick fills half of it.
    return PlaceSignal(
        ts_event=1_000,
        ts_init=1_000,
        strategy_id=strategy_id,
        symbol="BTC",
        seq=1,
        side=side,
        quantity=Decimal(quantity),
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        price=Decimal("43000" if side is Side.BUY else "41000"),
        reduce_only=reduce_only,
    )


def _size(engine: _Engine, strategy_id: str | None) -> Decimal | None:
    view = engine.checks.portfolio.position("BTC", strategy_id=strategy_id)
    return None if view is None else view.size


def _sizes(engine: _Engine) -> tuple[Decimal | None, Decimal | None]:
    """The strategy's size, then the unattributed partition's (``None`` in memory)."""
    return _size(engine, "trivial"), _size(engine, None)


def test_a_flatten_sell_fills_in_two_parts_and_every_partition_reads_flat() -> None:
    # The strategy is long 0.02 and a hand buy left the unattributed partition
    # long 0.01. A flatten sell of 0.03 fills 0.015 and then 0.015.
    engine = _engine()
    flatten = derive_cloid(f"{OPERATOR}:BTC:1")

    async def scenario() -> list[tuple[Decimal | None, Decimal | None]]:
        await engine.bus.publish(_tick("42000"))
        await engine.bus.publish(_crossing_limit("trivial", side=Side.BUY, quantity="0.02"))
        await engine.bus.publish(_tick("42001"))
        engine.checks.checkpoint_heal(
            (
                ReconciliationFill(
                    symbol="BTC",
                    side=Side.BUY,
                    quantity=Decimal("0.01"),
                    price=Decimal("42000"),
                    ts_ns=1_000,
                ),
            )
        )
        sizes = [_sizes(engine)]
        await engine.bus.publish(
            _crossing_limit(OPERATOR, side=Side.SELL, quantity="0.03", reduce_only=True)
        )
        sizes.append(_sizes(engine))
        await engine.bus.publish(_tick("42002"))
        sizes.append(_sizes(engine))
        return sizes

    sizes = asyncio.run(scenario())

    assert sizes == [
        (Decimal("0.02"), Decimal("0.01")),
        # 15 of 30 lots, handed out 2 to 1 by size.
        (Decimal("0.01"), Decimal("0.005")),
        (Decimal("0"), Decimal("0")),
    ]
    assert engine.store.get_order(flatten).state is OrderState.FILLED  # type: ignore[union-attr]
    # The operator never owns a partition.
    assert _size(engine, OPERATOR) is None


@pytest.mark.parametrize(
    ("held", "side", "quantity", "reason"),
    [
        (None, Side.SELL, "0.01", "a flatten order needs a net position to close"),
        ("0.02", Side.BUY, "0.01", "a flatten order must trade against the net position"),
        ("0.02", Side.SELL, "0.03", "a flatten order may not trade more than the net position"),
    ],
)
def test_a_flatten_order_its_fills_could_not_split_is_denied_before_the_send(
    held: str | None, side: Side, quantity: str, reason: str
) -> None:
    # The split assumes the order closes the net its basis holds. When the books
    # lag the venue, that can fail, and the fill would flip a partition or crash
    # the fill write on every boot. So the order never goes out.
    engine = _engine()
    denials: list[OrderDenied] = []

    async def record(event: OrderEvent) -> None:
        if isinstance(event, OrderDenied):
            denials.append(event)

    engine.bus.subscribe(OrderEvent, record)

    async def scenario() -> None:
        await engine.bus.publish(_tick("42000"))
        if held is not None:
            await engine.bus.publish(_crossing_limit("trivial", side=Side.BUY, quantity=held))
            await engine.bus.publish(_tick("42001"))
        await engine.bus.publish(
            _crossing_limit(OPERATOR, side=side, quantity=quantity, reduce_only=True)
        )

    asyncio.run(scenario())

    flatten = derive_cloid(f"{OPERATOR}:BTC:1")
    assert [(d.cloid, d.reason) for d in denials] == [(flatten, reason)]
    assert engine.store.get_order(flatten).state is OrderState.DENIED  # type: ignore[union-attr]
    assert engine.store.get_order(flatten).split_basis is None  # type: ignore[union-attr]


def _life(store: SQLiteStore, venue: PaperExchange, clock: ManualClock) -> _Engine:
    """One engine process over ``store``. The venue is passed in because it outlives us."""
    bus = InMemoryBus()
    checks = Checkpointer(
        spec=AccountSpec(account_id="paper-default", genesis_collateral=GENESIS),
        store=store,
        clock=clock,
        specs={"BTC": _SPEC},
    )
    checks.recover()
    manager = ExecutionManager(
        bus=bus,
        exchange=venue,
        checkpointer=checks,
        guard=RealGuard(specs={"BTC": _SPEC}, store=store, clock=clock),
    )
    bus.subscribe(Signal, manager.on_signal)
    bus.subscribe(ExecutionReport, manager.on_execution_report)
    return _Engine(bus, store, checks)


def test_reconcile_after_a_restart_books_the_rest_of_a_flatten_order_by_the_same_split(
    tmp_path: Path,
) -> None:
    # The same book as above. The first half of the flatten sell books live.
    # Then the engine dies, the second half fills at the venue, and reconcile
    # books it in the next life.
    clock = ManualClock(start_ns=1_000)
    path = tmp_path / "saga.db"
    open_store = [SQLiteStore(path)]
    # The venue reads the ledger through whichever store is open, and reports
    # on its own bus. The link to the engine dies with the engine.
    venue_bus = InMemoryBus()
    link: list[InMemoryBus] = []
    venue = PaperExchange(
        bus=venue_bus,
        clock=clock,
        fill_model=StochasticFillModel(
            rng=random.Random(0),
            clock=clock,
            params=StochasticParams(partial_fill_fraction=Decimal("0.5")),
        ),
        genesis_collateral=GENESIS,
        account_net=lambda: paper_store_reads(open_store[0]).account_net(),
        applied_fills=lambda cloid: paper_store_reads(open_store[0]).applied_fills(cloid),
    )

    async def forward(report: ExecutionReport) -> None:
        for bus in link:
            await bus.publish(report)

    venue_bus.subscribe(ExecutionReport, forward)
    flatten = derive_cloid(f"{OPERATOR}:BTC:1")

    async def scenario() -> _Engine:
        first = _life(open_store[0], venue, clock)
        link.append(first.bus)
        await venue_bus.publish(_tick("42000"))
        await first.bus.publish(_crossing_limit("trivial", side=Side.BUY, quantity="0.02"))
        await venue_bus.publish(_tick("42001"))
        first.checks.checkpoint_heal(
            (
                ReconciliationFill(
                    symbol="BTC",
                    side=Side.BUY,
                    quantity=Decimal("0.01"),
                    price=Decimal("42000"),
                    ts_ns=1_000,
                ),
            )
        )
        await first.bus.publish(
            _crossing_limit(OPERATOR, side=Side.SELL, quantity="0.03", reduce_only=True)
        )
        assert _sizes(first) == (Decimal("0.01"), Decimal("0.005"))

        # The crash. The rest fills while nothing is listening.
        link.clear()
        first.store.close()
        open_store[0] = SQLiteStore(path)
        await venue_bus.publish(_tick("42002"))

        second = _life(open_store[0], venue, clock)
        reconciler = Reconciler(
            bus=second.bus,
            clock=clock,
            exchange=venue,
            cache=second.checks.cache,
            config=ReconcileConfig(),
        )
        assert await reconciler.reconcile_startup()
        return second

    second = asyncio.run(scenario())

    assert _sizes(second) == (Decimal("0"), Decimal("0"))
    assert second.store.get_order(flatten).state is OrderState.FILLED  # type: ignore[union-attr]
    assert _size(second, OPERATOR) is None
    second.store.close()
