"""A flatten order's fills split over every partition, on the real paper pipeline (ADR-0054).

The order is owned by ``__operator__``, which never owns a partition. Each fill moves the
strategy's partition and the unattributed one toward zero, so both read flat at a full fill.
"""

import asyncio
import random
from dataclasses import dataclass
from decimal import Decimal

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
