"""Operator flatten (ADR-0053, ADR-0054, ADR-0056, ADR-0060): the ``Engine`` as a
one-shot exit run that closes every position.

The real paper path, with no strategy: ``PaperExchange`` over ``InMemoryBus`` and
``SQLiteStore``, fed by ``ReplayFeed`` on ``ManualClock``.
"""

import asyncio
import json
from collections.abc import Callable, Iterable, Sequence
from decimal import Decimal
from functools import partial
from pathlib import Path

import pytest
from ledgers import GENESIS
from structlog.typing import EventDict
from venue_doubles import VenueLink

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.feed import ReplayFeed
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.adapters.store import SQLiteStore
from tickwright.app.build import paper_store_reads
from tickwright.domain import (
    OPERATOR,
    AggressorSide,
    Clock,
    Exchange,
    ExecutionReport,
    InstrumentSpec,
    MarketTick,
    Order,
    OrderRef,
    OrderState,
    OrderType,
    PlaceOrder,
    PlaceSignal,
    Side,
    Signal,
    TimeInForce,
    VenueOpenOrder,
    VenueOrderView,
    VenueReadFailure,
    derive_cloid,
)
from tickwright.engine.checkpoint import Checkpointer
from tickwright.engine.execution import ExecutionManager
from tickwright.engine.exit_run import Flatten
from tickwright.engine.guard import NoopGuard, RealGuard
from tickwright.engine.runner import Engine
from tickwright.observability.testing import capture_events

_SPECS = {
    symbol: InstrumentSpec(symbol=symbol, sz_decimals=3, max_decimals=6, min_notional=Decimal("0"))
    for symbol in ("BTC", "ETH")
}

# Each symbol's replay rows, in file order. Paper fills a market order at the last one.
_ROWS = {"BTC": ("42000", "43000", "44000"), "ETH": ("3000", "3100", "3200")}


def _ticks(path: Path, replayed: Iterable[str]) -> Path:
    rows = [
        {
            "symbol": symbol,
            "price": price,
            "size": "1",
            "aggressor_side": "buy",
            "trade_id": f"{symbol}-{n}",
            "ts_event": 1_000 * n,
        }
        for n, (symbol, price) in enumerate(
            ((symbol, price) for symbol in replayed for price in _ROWS[symbol]), start=1
        )
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def _paper(bus: InMemoryBus, clock: ManualClock, store: SQLiteStore) -> PaperExchange:
    reads = paper_store_reads(store)
    return PaperExchange(
        bus=bus,
        clock=clock,
        fill_model=ImmediateFillModel(),
        genesis_collateral=GENESIS,
        instrument_specs=_SPECS,
        account_net=reads.account_net,
        applied_fills=reads.applied_fills,
    )


def _hold(path: Path, sizes: dict[str, str]) -> None:
    """Open each signed size as the strategy ``trivial``, at the symbol's first row price.

    Seeded through the engine's own order path on a separate bus, as an earlier run would.
    """
    bus = InMemoryBus()
    clock = ManualClock()
    with SQLiteStore(path) as store:
        paper = _paper(bus, clock, store)
        checks = Checkpointer(spec=paper.account_spec(), store=store, clock=clock, specs=_SPECS)
        checks.recover()
        manager = ExecutionManager(bus=bus, exchange=paper, checkpointer=checks, guard=NoopGuard())
        bus.subscribe(Signal, manager.on_signal)
        bus.subscribe(ExecutionReport, manager.on_execution_report)

        async def go() -> None:
            for seq, (symbol, size) in enumerate(sizes.items(), start=1):
                await bus.publish(_tick(symbol, _ROWS[symbol][0]))
                await bus.publish(
                    PlaceSignal(
                        ts_event=0,
                        ts_init=0,
                        strategy_id="trivial",
                        symbol=symbol,
                        seq=seq,
                        side=Side.BUY if Decimal(size) > 0 else Side.SELL,
                        quantity=abs(Decimal(size)),
                        order_type=OrderType.MARKET,
                        time_in_force=TimeInForce.IOC,
                    )
                )

        asyncio.run(go())


def _tick(symbol: str, price: str) -> MarketTick:
    return MarketTick(
        ts_event=0,
        ts_init=0,
        symbol=symbol,
        price=Decimal(price),
        size=Decimal("1"),
        aggressor_side=AggressorSide.BUY,
        trade_id=f"seed-{symbol}",
        seq=0,
    )


def _flatten(
    tmp_path: Path,
    *,
    guarded: bool = True,
    link: Callable[[PaperExchange], Exchange] | None = None,
    replayed: Iterable[str] = tuple(_ROWS),
    resting: bool = False,
) -> tuple[int, list[EventDict]]:
    """Run flatten over paper and the ``_ROWS`` replay file.

    ``guarded`` gives the engine a real guard. ``link`` puts a venue double in
    front of paper. ``replayed`` picks the symbols the file has rows for.
    ``resting`` rests one BTC buy far below the replay, as ``_RESTING``.
    Returns the exit code and every event the run emitted.
    """
    bus = InMemoryBus()
    clock = ManualClock()
    store = SQLiteStore(tmp_path / "saga.db")
    paper = _paper(bus, clock, store)
    engine = Engine(
        bus=bus,
        clock=clock,
        store=store,
        exchange=paper if link is None else link(paper),
        feed=ReplayFeed(path=_ticks(tmp_path / "ticks.jsonl", replayed), bus=bus, clock=clock),
        guard=RealGuard(specs=_SPECS, store=store, clock=clock) if guarded else None,
        exit_job=Flatten(),
    )

    async def go() -> int:
        if resting:
            await _rest(bus, paper, store, clock)
        return await asyncio.wait_for(engine.run(), timeout=5)

    with capture_events() as logs:
        exit_code = asyncio.run(go())
    return exit_code, logs


_RESTING = derive_cloid("resting:BTC:1")


async def _rest(bus: InMemoryBus, paper: PaperExchange, store: SQLiteStore, clock: Clock) -> None:
    """Rest ``_RESTING`` on paper, with its saga in the store.

    Paper keeps its book in memory only (#484), so the order goes on the run's own
    exchange. The saga is left ``SUBMITTED``, and the boot barrier heals it to ``LIVE``.
    """
    # Paper prices a LIMIT against the last tick, so it needs one first.
    await bus.publish(_tick("BTC", "42000"))
    order = PlaceOrder(
        cloid=_RESTING,
        symbol="BTC",
        side=Side.BUY,
        quantity=Decimal("0.01"),
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        price=Decimal("30000"),
    )
    await paper.place(order)
    checks = Checkpointer(spec=paper.account_spec(), store=store, clock=clock, specs=_SPECS)
    checks.recover()
    checks.checkpoint(
        Order(
            cloid=_RESTING,
            strategy_id="resting",
            signal_id="resting:BTC:1",
            symbol="BTC",
            side=Side.BUY,
            quantity=order.quantity,
            order_type=OrderType.LIMIT,
            state=OrderState.SUBMITTED,
            created_ts_ns=0,
        )
    )


def _partitions(tmp_path: Path) -> dict[tuple[str | None, str], tuple[Decimal, Decimal]]:
    """Each partition's signed size and realized P&L, as the run left them in the store."""
    with SQLiteStore(tmp_path / "saga.db") as store:
        return {
            (row.strategy_id, row.symbol): (row.signed_size, row.realized_pnl)
            for row in store.all_positions()
        }


def test_flatten_with_nothing_to_close_still_trips_the_kill_switch(tmp_path: Path) -> None:
    # The trip records what the operator meant, not what flatten found (ADR-0053).
    exit_code, logs = _flatten(tmp_path)

    assert exit_code == 0
    events = [log["event"] for log in logs]
    assert "guard.kill_switch_tripped" in events
    assert "guard.kill_switch_reset" not in events
    assert logs[-1]["event"] == "exit.finished"
    assert logs[-1]["job"] == "flatten"
    assert logs[-1]["outcome"] == "done"
    assert logs[-1]["left"] == "none"
    after = SQLiteStore(tmp_path / "saga.db")
    try:
        state = after.load_kill_switch()
    finally:
        after.close()
    assert state is not None
    assert state.tripped
    assert state.reason == "flatten: operator exit"


def test_flatten_under_a_noop_guard_warns_and_still_closes(tmp_path: Path) -> None:
    # A noop guard has no kill switch to trip, so the next boot trades at once (ADR-0053).
    _hold(tmp_path / "saga.db", {"BTC": "0.02"})

    exit_code, logs = _flatten(tmp_path, guarded=False)

    assert exit_code == 0
    unguarded = [log for log in logs if log["event"] == "exit.unguarded"]
    assert len(unguarded) == 1
    assert "restart" in unguarded[0]["warning"]
    assert logs[-1]["event"] == "exit.finished"
    assert logs[-1]["outcome"] == "done"
    assert _partitions(tmp_path) == {("trivial", "BTC"): (Decimal("0"), Decimal("40"))}


def test_flatten_closes_a_long_at_the_last_replay_row_and_the_partition_reads_flat(
    tmp_path: Path,
) -> None:
    # Opened at 42000. The replay ends at 44000, so 0.02 realizes exactly 40 (ADR-0059).
    # The run trips the kill switch first, and the close still goes out (ADR-0053).
    _hold(tmp_path / "saga.db", {"BTC": "0.02"})

    exit_code, logs = _flatten(tmp_path)

    assert exit_code == 0
    assert logs[-1]["event"] == "exit.finished"
    assert logs[-1]["outcome"] == "done"
    assert logs[-1]["left"] == "none"
    assert _partitions(tmp_path) == {("trivial", "BTC"): (Decimal("0"), Decimal("40"))}


def test_a_held_symbol_the_replay_never_prices_stops_the_run_with_its_size_left(
    tmp_path: Path,
) -> None:
    # Paper has no price to fill BTC at, so the close cannot fill. The final read says so.
    _hold(tmp_path / "saga.db", {"BTC": "0.02"})

    exit_code, logs = _flatten(tmp_path, replayed=("ETH",))

    assert exit_code == 1
    assert logs[-1]["event"] == "exit.finished"
    assert logs[-1]["outcome"] == "stopped"
    assert logs[-1]["left"] == "BTC=0.02"
    assert _partitions(tmp_path) == {("trivial", "BTC"): (Decimal("0.02"), Decimal("0"))}


def test_flatten_cancels_a_resting_order_before_it_closes_a_long_and_a_short(
    tmp_path: Path,
) -> None:
    # A resting order could fill after the close and reopen exposure (ADR-0060).
    # The short opened at 3000 and closes at 3200, so 0.5 realizes exactly -100.
    _hold(tmp_path / "saga.db", {"BTC": "0.02", "ETH": "-0.5"})

    exit_code, logs = _flatten(tmp_path, resting=True)

    assert exit_code == 0
    assert logs[-1]["event"] == "exit.finished"
    assert logs[-1]["outcome"] == "done"
    assert logs[-1]["left"] == "none"
    events = [log["event"] for log in logs]
    first_close = next(
        n
        for n, log in enumerate(logs)
        if log["event"] == "order.placed" and log["signal_id"].startswith(OPERATOR)
    )
    assert events.index("order.cancelled") < first_close
    assert _partitions(tmp_path) == {
        ("trivial", "BTC"): (Decimal("0"), Decimal("40")),
        ("trivial", "ETH"): (Decimal("0"), Decimal("-100")),
    }
    with SQLiteStore(tmp_path / "saga.db") as store:
        resting = store.get_order(_RESTING)
    assert resting is not None
    assert resting.state is OrderState.CANCELLED


class _DropsEveryCancel(VenueLink):
    """Every cancel is lost on the way, so the order keeps resting."""

    async def cancel(self, refs: Sequence[OrderRef]) -> None:
        return None


def test_an_order_that_stays_resting_stops_flatten_before_any_close(tmp_path: Path) -> None:
    # The order could fill after the close and reopen exposure (ADR-0060).
    _hold(tmp_path / "saga.db", {"BTC": "0.02"})

    exit_code, logs = _flatten(tmp_path, link=_DropsEveryCancel, resting=True)

    assert exit_code == 1
    assert "cancel_all.orders_remain" in [log["event"] for log in logs]
    assert not [log for log in logs if str(log.get("signal_id", "")).startswith(OPERATOR)]
    assert logs[-1]["event"] == "exit.finished"
    assert logs[-1]["outcome"] == "stopped"
    assert logs[-1]["left"] == _RESTING
    assert _partitions(tmp_path) == {("trivial", "BTC"): (Decimal("0.02"), Decimal("0"))}


class _FailsOneRead(VenueLink):
    """The ``nth`` call of ``read`` fails. Every other read is the real venue's answer."""

    def __init__(self, venue: Exchange, *, read: str, nth: int) -> None:
        super().__init__(venue)
        self._read = read
        self._nth = nth
        self._calls = {"fetch_open_orders": 0, "fetch_positions": 0}

    def _fails(self, read: str) -> bool:
        self._calls[read] += 1
        return read == self._read and self._calls[read] == self._nth

    async def fetch_open_orders(self) -> list[VenueOpenOrder] | VenueReadFailure:
        if self._fails("fetch_open_orders"):
            return VenueReadFailure.SEND_FAILED
        return await super().fetch_open_orders()

    async def fetch_positions(self) -> dict[str, Decimal] | VenueReadFailure:
        if self._fails("fetch_positions"):
            return VenueReadFailure.SEND_FAILED
        return await super().fetch_positions()


@pytest.mark.parametrize(
    ("read", "nth"),
    [
        pytest.param("fetch_positions", 1, id="positions-before-any-close"),
        pytest.param("fetch_open_orders", 2, id="final-orders"),
        pytest.param("fetch_positions", 2, id="final-positions"),
    ],
)
def test_a_failed_venue_read_stops_flatten_and_never_reads_as_flat(
    tmp_path: Path, read: str, nth: int
) -> None:
    # A failed read is not an empty account (ADR-0011 inv 1), so the run cannot be done.
    _hold(tmp_path / "saga.db", {"BTC": "0.02"})

    exit_code, logs = _flatten(tmp_path, link=partial(_FailsOneRead, read=read, nth=nth))

    assert exit_code == 1
    assert logs[-1]["event"] == "exit.finished"
    assert logs[-1]["outcome"] == "stopped"
    assert logs[-1]["left"] == f"read_failed: {VenueReadFailure.SEND_FAILED.value}"


class _DropsEveryPlace(VenueLink):
    """Every send is lost on the way, so the venue never hears of the order."""

    async def place(self, order: PlaceOrder) -> None:
        return None


def test_a_lost_close_is_reconciled_to_a_verdict_and_the_run_stops_with_the_size_left(
    tmp_path: Path,
) -> None:
    # Only reconcile passes can tell the saga the send never landed (ADR-0011).
    _hold(tmp_path / "saga.db", {"BTC": "0.02"})

    exit_code, logs = _flatten(tmp_path, link=_DropsEveryPlace)

    assert exit_code == 1
    assert logs[-1]["event"] == "exit.finished"
    assert logs[-1]["outcome"] == "stopped"
    assert logs[-1]["left"] == "BTC=0.02"
    with SQLiteStore(tmp_path / "saga.db") as store:
        close = store.get_order(derive_cloid(f"{OPERATOR}:BTC:1"))
    assert close is not None
    assert close.state is OrderState.FAILED


class _LostAndUnreadable(_DropsEveryPlace):
    """The send is lost and every order read fails, so no pass can ever decide it."""

    def __init__(self, venue: Exchange) -> None:
        super().__init__(venue)
        self.reads = 0

    async def fetch_order(self, ref: OrderRef) -> VenueOrderView | VenueReadFailure:
        self.reads += 1
        # A hang on the virtual clock never yields, so the test bounds it here.
        assert self.reads < 10, "flatten kept reading a venue that cannot answer"
        return VenueReadFailure.SEND_FAILED


def test_a_failed_reconcile_pass_ends_the_wait_and_the_final_read_decides(
    tmp_path: Path,
) -> None:
    # The saga stays open for the next boot to resume (#474). The run never hangs.
    _hold(tmp_path / "saga.db", {"BTC": "0.02"})

    exit_code, logs = _flatten(tmp_path, link=_LostAndUnreadable)

    assert exit_code == 1
    assert logs[-1]["outcome"] == "stopped"
    assert logs[-1]["left"] == "BTC=0.02"
