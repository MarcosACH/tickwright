"""Operator flatten (ADR-0053, ADR-0054, ADR-0056, ADR-0060): the ``Engine`` as a
one-shot exit run that closes every position.

The real paper path, with no strategy: ``PaperExchange`` over ``InMemoryBus`` and
``SQLiteStore``, fed by ``ReplayFeed`` on ``ManualClock``.
"""

import asyncio
import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import replace
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
    Event,
    EventBus,
    Exchange,
    ExecutionReport,
    ExternalOrderRef,
    Handler,
    InstrumentSpec,
    MarketTick,
    Order,
    OrderRef,
    OrderState,
    OrderStatusReport,
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
from tickwright.engine.exit_run import ExitJob, Flatten, OperatorCancelAll
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


def _paper(bus: EventBus, clock: ManualClock, store: SQLiteStore) -> PaperExchange:
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


def _hold(path: Path, sizes: dict[str, str], *, strategy_id: str = "trivial", row: int = 0) -> None:
    """Open each signed size as ``strategy_id``, at the price of the symbol's ``row``.

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
                await bus.publish(_tick(symbol, _ROWS[symbol][row]))
                await bus.publish(
                    PlaceSignal(
                        ts_event=0,
                        ts_init=0,
                        strategy_id=strategy_id,
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
    clock: ManualClock | None = None,
    bus: EventBus | None = None,
) -> tuple[int, list[EventDict]]:
    """Run flatten over paper and the ``_ROWS`` replay file.

    ``guarded`` gives the engine a real guard. ``link`` puts a venue double in
    front of paper. ``replayed`` picks the symbols the file has rows for.
    ``resting`` rests one BTC buy far below the replay, as ``_RESTING``.
    ``clock`` lets a test read the virtual time the run ended at. ``bus`` lets
    a venue double publish its own reports.
    Returns the exit code and every event the run emitted.
    """
    bus = bus or InMemoryBus()
    clock = clock or ManualClock()
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


async def _rest(bus: EventBus, paper: PaperExchange, store: SQLiteStore, clock: Clock) -> None:
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


def test_a_held_symbol_the_replay_never_prices_gives_up_after_three_dry_attempts(
    tmp_path: Path,
) -> None:
    # No price can come after the replay ends, so every attempt is dry (ADR-0056).
    # The replay ends at 3000 ns. Two 2 second waits sit between the three attempts.
    _hold(tmp_path / "saga.db", {"BTC": "0.02"})
    clock = ManualClock()

    exit_code, logs = _flatten(tmp_path, replayed=("ETH",), clock=clock)

    assert exit_code == 1
    dry = [log for log in logs if log["event"] == "flatten.dry_attempt"]
    assert [(log["symbol"], log["count"], log["reason"]) for log in dry] == [
        ("BTC", 1, "no_price"),
        ("BTC", 2, "no_price"),
        ("BTC", 3, "no_price"),
    ]
    gave_up = [log for log in logs if log["event"] == "flatten.gave_up"]
    assert [(log["symbol"], log["size"]) for log in gave_up] == [("BTC", "0.02")]
    assert clock.timestamp_ns() == 3_000 + 4_000_000_000
    assert logs[-1]["event"] == "exit.finished"
    assert logs[-1]["outcome"] == "stopped"
    assert logs[-1]["left"] == "BTC=0.02"
    assert _partitions(tmp_path) == {("trivial", "BTC"): (Decimal("0.02"), Decimal("0"))}


class _ThinBook(VenueLink):
    """Each close fills only what the book holds, and the venue cancels the rest.

    ``fills`` gives the size each send finds, in order. Zero fills nothing.
    """

    def __init__(self, venue: Exchange, *, bus: EventBus, fills: Sequence[str]) -> None:
        super().__init__(venue)
        self._bus = bus
        self._fills = [Decimal(fill) for fill in fills]

    async def place(self, order: PlaceOrder) -> None:
        fill = self._fills.pop(0)
        if fill:
            await super().place(replace(order, quantity=fill))
        # An IOC never rests, so whatever did not fill ends cancelled.
        await self._bus.publish(
            OrderStatusReport(
                ts_event=0,
                ts_init=0,
                cloid=order.cloid,
                symbol=order.symbol,
                status=OrderState.CANCELLED,
            )
        )


def test_a_fill_between_dry_attempts_resets_the_count(tmp_path: Path) -> None:
    # Two dry attempts, then a partial fill, then three more dry ones. Without the
    # reset, flatten would give up on the third dry attempt, before its fifth send.
    _hold(tmp_path / "saga.db", {"BTC": "0.02"})
    bus = InMemoryBus()
    link = partial(_ThinBook, bus=bus, fills=("0", "0", "0.01", "0", "0", "0"))

    exit_code, logs = _flatten(tmp_path, link=link, bus=bus)

    assert exit_code == 1
    dry = [log for log in logs if log["event"] == "flatten.dry_attempt"]
    assert [(log["count"], log["reason"]) for log in dry] == [
        (1, "no_fill"),
        (2, "no_fill"),
        (1, "no_fill"),
        (2, "no_fill"),
        (3, "no_fill"),
    ]
    gave_up = [log for log in logs if log["event"] == "flatten.gave_up"]
    assert [(log["symbol"], Decimal(log["size"])) for log in gave_up] == [("BTC", Decimal("0.01"))]
    symbol, left = logs[-1]["left"].split("=")
    assert (symbol, Decimal(left)) == ("BTC", Decimal("0.01"))


class _ReadsMore(VenueLink):
    """The venue reads more size than the ledger holds, by ``extra``, on every read."""

    def __init__(self, venue: Exchange, *, symbol: str, extra: str) -> None:
        super().__init__(venue)
        self._symbol = symbol
        self._extra = Decimal(extra)

    async def fetch_positions(self) -> dict[str, Decimal] | VenueReadFailure:
        positions = await super().fetch_positions()
        if isinstance(positions, VenueReadFailure):
            return positions
        return {**positions, self._symbol: positions.get(self._symbol, Decimal(0)) + self._extra}


def test_a_size_gap_that_never_heals_gives_up_with_no_close(tmp_path: Path) -> None:
    # A close is split across the partitions, so it may only go out when they sum
    # to the venue size (ADR-0054). Paper has no account truth to heal the gap with.
    _hold(tmp_path / "saga.db", {"BTC": "0.02"})

    exit_code, logs = _flatten(tmp_path, link=partial(_ReadsMore, symbol="BTC", extra="0.01"))

    assert exit_code == 1
    dry = [log for log in logs if log["event"] == "flatten.dry_attempt"]
    assert [(log["count"], log["reason"]) for log in dry] == [
        (1, "size_gap"),
        (2, "size_gap"),
        (3, "size_gap"),
    ]
    gave_up = [log for log in logs if log["event"] == "flatten.gave_up"]
    assert [(log["symbol"], log["size"]) for log in gave_up] == [("BTC", "0.03")]
    assert not [log for log in logs if str(log.get("signal_id", "")).startswith(OPERATOR)]
    assert logs[-1]["left"] == "BTC=0.03"
    assert _partitions(tmp_path) == {("trivial", "BTC"): (Decimal("0.02"), Decimal("0"))}


class _HoldsOneOrder:
    """An ``InMemoryBus`` that holds every report for ``cloid`` until ``drain()``.

    Those are reports the venue published that the engine has not applied yet.
    Kafka can leave them in flight, in order, and ``drain()`` waits for them.
    """

    def __init__(self, cloid: str) -> None:
        self._bus = InMemoryBus()
        self._cloid = cloid
        self._held: list[ExecutionReport] = []

    def subscribe[E: Event](self, event_type: type[E], handler: Handler[E]) -> None:
        self._bus.subscribe(event_type, handler)

    async def start(self) -> None:
        await self._bus.start()

    async def publish(self, event: Event) -> None:
        if isinstance(event, ExecutionReport) and event.cloid == self._cloid:
            self._held.append(event)
            return
        await self._bus.publish(event)

    async def drain(self) -> None:
        held, self._held = self._held, []
        for event in held:
            await self._bus.publish(event)
        await self._bus.drain()

    async def close(self) -> None:
        await self._bus.close()


class _FillsAsItCancels(VenueLink):
    """The market trades through each resting BTC buy just before its cancel lands."""

    def __init__(self, venue: Exchange, *, bus: EventBus) -> None:
        super().__init__(venue)
        self._bus = bus

    async def cancel(self, refs: Sequence[OrderRef | ExternalOrderRef]) -> None:
        await self._bus.publish(_tick("BTC", "30000"))
        await super().cancel(refs)


def test_a_size_gap_from_a_fill_still_in_flight_closes_after_a_drain(tmp_path: Path) -> None:
    # The resting buy fills on the venue as cancel all runs, but its fill is still
    # in flight. The drain applies it, so the partitions sum to the venue size
    # again, and one close covers both (ADR-0054).
    _hold(tmp_path / "saga.db", {"BTC": "0.02"})
    bus = _HoldsOneOrder(_RESTING)

    exit_code, logs = _flatten(
        tmp_path, link=partial(_FillsAsItCancels, bus=bus), bus=bus, resting=True
    )

    assert exit_code == 0
    assert not [log for log in logs if log["event"] == "flatten.dry_attempt"]
    assert logs[-1]["outcome"] == "done"
    # Both close at 44000. One opened at 42000, the other filled at 30000.
    assert _partitions(tmp_path) == {
        ("trivial", "BTC"): (Decimal("0"), Decimal("40")),
        ("resting", "BTC"): (Decimal("0"), Decimal("140")),
    }


class _SoldByHand(_ThinBook):
    """The first close fills nothing, and then the venue reads BTC flat.

    The operator sold it by hand in the venue UI, so the books never see that trade.
    """

    def __init__(self, venue: Exchange, *, bus: EventBus) -> None:
        super().__init__(venue, bus=bus, fills=("0",))
        self._sold = False

    async def place(self, order: PlaceOrder) -> None:
        await super().place(order)
        self._sold = True

    async def fetch_positions(self) -> dict[str, Decimal] | VenueReadFailure:
        positions = await super().fetch_positions()
        if isinstance(positions, VenueReadFailure) or not self._sold:
            return positions
        return {symbol: size for symbol, size in positions.items() if symbol != "BTC"}


def test_a_venue_that_reads_flat_moves_the_leftover_to_unattributed_at_the_entry_price(
    tmp_path: Path,
) -> None:
    # No order can close what the venue no longer holds. The strategy's size moves at
    # its own entry price, so its realized PnL stays 0. At the 44000 mark it would be
    # 40. The unattributed size keeps the gap visible (ADR-0054, ADR-0056).
    _hold(tmp_path / "saga.db", {"BTC": "0.02"})
    bus = InMemoryBus()

    exit_code, logs = _flatten(tmp_path, link=partial(_SoldByHand, bus=bus), bus=bus)

    assert exit_code == 0
    assert logs[-1]["outcome"] == "done"
    assert logs[-1]["left"] == "none"
    assert _partitions(tmp_path) == {
        ("trivial", "BTC"): (Decimal("0"), Decimal("0")),
        (None, "BTC"): (Decimal("0.02"), Decimal("0")),
    }
    with SQLiteStore(tmp_path / "saga.db") as store:
        unattributed = [row for row in store.all_positions() if row.strategy_id is None]
    assert [row.entry_price for row in unattributed] == [Decimal("42000")]


class _SoldAfterTheBootRead(VenueLink):
    """The venue reads BTC flat on every positions read after flatten's first.

    The operator sold it by hand while flatten waited, so the books never see that trade.
    """

    def __init__(self, venue: Exchange) -> None:
        super().__init__(venue)
        self._reads = 0

    async def fetch_positions(self) -> dict[str, Decimal] | VenueReadFailure:
        self._reads += 1
        positions = await super().fetch_positions()
        if isinstance(positions, VenueReadFailure) or self._reads == 1:
            return positions
        return {symbol: size for symbol, size in positions.items() if symbol != "BTC"}


def test_a_venue_that_reads_flat_with_no_price_ends_the_loop_with_no_give_up(
    tmp_path: Path,
) -> None:
    # An attempt with no price sends no order, but it still reads the venue before
    # it counts. A flat venue ends flatten (ADR-0056), so the leftover moves and
    # no dry attempt is counted.
    _hold(tmp_path / "saga.db", {"BTC": "0.02"})

    exit_code, logs = _flatten(tmp_path, link=_SoldAfterTheBootRead, replayed=("ETH",))

    assert exit_code == 0
    assert not [log for log in logs if log["event"].startswith("flatten.")]
    assert logs[-1]["outcome"] == "done"
    assert _partitions(tmp_path) == {
        ("trivial", "BTC"): (Decimal("0"), Decimal("0")),
        (None, "BTC"): (Decimal("0.02"), Decimal("0")),
    }


def test_two_strategies_of_opposite_sign_leave_one_unattributed_piece_at_their_cost(
    tmp_path: Path,
) -> None:
    # Each strategy leaves at its own entry, so neither books realized PnL. The
    # unattributed side takes their net at the cost they paid together, so no
    # cash moves (ADR-0054). That price need not be one the market traded:
    # 0.02 at 42000 less 0.01 at 44000 is 400, over a size of 0.01.
    _hold(tmp_path / "saga.db", {"BTC": "0.02"})
    _hold(tmp_path / "saga.db", {"BTC": "-0.01"}, strategy_id="other", row=2)

    exit_code, logs = _flatten(tmp_path, link=_SoldAfterTheBootRead, replayed=("ETH",))

    assert exit_code == 0
    assert logs[-1]["outcome"] == "done"
    assert _partitions(tmp_path) == {
        ("trivial", "BTC"): (Decimal("0"), Decimal("0")),
        ("other", "BTC"): (Decimal("0"), Decimal("0")),
        (None, "BTC"): (Decimal("0.01"), Decimal("0")),
    }
    with SQLiteStore(tmp_path / "saga.db") as store:
        unattributed = [row for row in store.all_positions() if row.strategy_id is None]
    assert [row.entry_price for row in unattributed] == [Decimal("40000")]


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

    async def cancel(self, refs: Sequence[OrderRef | ExternalOrderRef]) -> None:
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
        # The second positions read is the one after the close.
        pytest.param("fetch_positions", 3, id="final-positions"),
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


class _LostOverANetwork(_DropsEveryPlace):
    """Every send is lost, and each order read waits on the network.

    ``places`` lists the symbol of each send in order. ``overlap`` is the most
    order reads that were in flight at once. A pass reads its orders one by one,
    so more than one means two passes overlapped.
    """

    def __init__(self, venue: Exchange) -> None:
        super().__init__(venue)
        self.places: list[str] = []
        self.overlap = 0
        self._in_flight = 0

    async def place(self, order: PlaceOrder) -> None:
        self.places.append(order.symbol)

    async def fetch_order(self, ref: OrderRef) -> VenueOrderView | VenueReadFailure:
        self._in_flight += 1
        self.overlap = max(self.overlap, self._in_flight)
        # The network round trip, where another loop gets to run.
        await asyncio.sleep(0)
        self._in_flight -= 1
        return await super().fetch_order(ref)


def test_two_held_symbols_retry_at_the_same_time_and_their_passes_never_overlap(
    tmp_path: Path,
) -> None:
    # One loop per symbol, all at the same time, and one reconcile pass at a time
    # under a lock. A loop that needs a pass waits for the one in progress.
    _hold(tmp_path / "saga.db", {"BTC": "0.02", "ETH": "0.5"})
    venues: list[_LostOverANetwork] = []

    def link(paper: PaperExchange) -> Exchange:
        venues.append(_LostOverANetwork(paper))
        return venues[0]

    exit_code, logs = _flatten(tmp_path, link=link)

    assert exit_code == 1
    [venue] = venues
    # Both symbols send a first close before either sends a second one.
    assert sorted(venue.places[:2]) == ["BTC", "ETH"]
    assert sorted(venue.places) == ["BTC"] * 3 + ["ETH"] * 3
    assert venue.overlap == 1
    gave_up = [log["symbol"] for log in logs if log["event"] == "flatten.gave_up"]
    assert sorted(gave_up) == ["BTC", "ETH"]


class _PlacesOverANetwork(VenueLink):
    """Each send waits on the network before the venue takes it, as an HTTP send does.

    ``places`` lists the symbol of each send in order.
    """

    def __init__(self, venue: Exchange) -> None:
        super().__init__(venue)
        self.places: list[str] = []

    async def place(self, order: PlaceOrder) -> None:
        self.places.append(order.symbol)
        # The network round trip, where another loop gets to run.
        await asyncio.sleep(0)
        await super().place(order)


def test_a_send_that_waits_on_the_network_gets_no_second_close_beside_it(
    tmp_path: Path,
) -> None:
    # While one loop's send waits, another loop may run. Its close must still be
    # handled before it looks for a verdict. Otherwise it reads no fill and sends
    # a second close beside the first (ADR-0056).
    _hold(tmp_path / "saga.db", {"BTC": "0.02", "ETH": "0.5"})
    venues: list[_PlacesOverANetwork] = []

    def link(paper: PaperExchange) -> Exchange:
        venues.append(_PlacesOverANetwork(paper))
        return venues[0]

    exit_code, logs = _flatten(tmp_path, link=link)

    assert exit_code == 0
    [venue] = venues
    assert sorted(venue.places) == ["BTC", "ETH"]
    assert not [log for log in logs if log["event"].startswith("flatten.")]
    assert _partitions(tmp_path) == {
        ("trivial", "BTC"): (Decimal("0"), Decimal("40")),
        ("trivial", "ETH"): (Decimal("0"), Decimal("100")),
    }


class _NeverHandlesCloses:
    """An ``InMemoryBus`` that takes every ``__operator__`` close and never handles it.

    ``closes`` counts them. This is a publish that returned before the engine handled it.
    """

    def __init__(self) -> None:
        self._bus = InMemoryBus()
        self.closes = 0

    def subscribe[E: Event](self, event_type: type[E], handler: Handler[E]) -> None:
        self._bus.subscribe(event_type, handler)

    async def start(self) -> None:
        await self._bus.start()

    async def publish(self, event: Event) -> None:
        if isinstance(event, PlaceSignal) and event.strategy_id == OPERATOR:
            self.closes += 1
            return
        await self._bus.publish(event)

    async def drain(self) -> None:
        await self._bus.drain()

    async def close(self) -> None:
        await self._bus.close()


def test_a_close_the_engine_never_handled_stops_the_run_with_no_second_close(
    tmp_path: Path,
) -> None:
    # With no saga there is no verdict to read. Reading it as a dry attempt would
    # send a second close, and the first could still reach the venue (ADR-0056).
    _hold(tmp_path / "saga.db", {"BTC": "0.02"})
    bus = _NeverHandlesCloses()

    exit_code, logs = _flatten(tmp_path, bus=bus)

    assert exit_code == 1
    assert bus.closes == 1
    assert not [log for log in logs if log["event"] == "flatten.dry_attempt"]
    assert [log for log in logs if log["event"] == "engine.faulted"]
    assert logs[-1]["outcome"] == "stopped"


def test_a_second_flatten_closes_with_a_seq_above_the_first_ones_close(tmp_path: Path) -> None:
    # The manager drops a signal id it has seen (ADR-0006). So a second flatten that
    # reused seq 1 would send nothing, and the size would stay open. The first one
    # spends seqs 1 to 3 on its three lost closes.
    _hold(tmp_path / "saga.db", {"BTC": "0.02"})
    assert _flatten(tmp_path, link=_DropsEveryPlace)[0] == 1

    exit_code, logs = _flatten(tmp_path)

    assert exit_code == 0
    assert logs[-1]["outcome"] == "done"
    assert _partitions(tmp_path) == {("trivial", "BTC"): (Decimal("0"), Decimal("40"))}
    with SQLiteStore(tmp_path / "saga.db") as store:
        close = store.get_order(derive_cloid(f"{OPERATOR}:BTC:4"))
    assert close is not None
    assert close.state is OrderState.FILLED


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


def test_three_failed_reconcile_passes_give_up_with_no_second_close(tmp_path: Path) -> None:
    # A failed read is a dry attempt (ADR-0060), so the run never hangs. The close
    # could still fill, so no second one goes beside it. The saga stays open for
    # the next boot to resume (#474).
    _hold(tmp_path / "saga.db", {"BTC": "0.02"})

    exit_code, logs = _flatten(tmp_path, link=_LostAndUnreadable)

    assert exit_code == 1
    dry = [log for log in logs if log["event"] == "flatten.dry_attempt"]
    assert [(log["count"], log["reason"]) for log in dry] == [
        (1, "read_failed"),
        (2, "read_failed"),
        (3, "read_failed"),
    ]
    assert [log["symbol"] for log in logs if log["event"] == "flatten.gave_up"] == ["BTC"]
    assert not [log for log in logs if log.get("signal_id") == f"{OPERATOR}:BTC:2"]
    assert logs[-1]["outcome"] == "stopped"
    assert logs[-1]["left"] == "BTC=0.02"


def test_a_failed_read_after_a_close_is_a_dry_attempt_and_the_next_read_decides(
    tmp_path: Path,
) -> None:
    # The close filled, but the read that would say so failed. Flatten waits and
    # reads again instead of stopping (ADR-0060). It sends no second close.
    _hold(tmp_path / "saga.db", {"BTC": "0.02"})

    # The first positions read is flatten's own. The second is the one after the close.
    exit_code, logs = _flatten(tmp_path, link=partial(_FailsOneRead, read="fetch_positions", nth=2))

    assert exit_code == 0
    dry = [log for log in logs if log["event"] == "flatten.dry_attempt"]
    assert [(log["count"], log["reason"]) for log in dry] == [(1, "read_failed")]
    assert not [log for log in logs if log.get("signal_id") == f"{OPERATOR}:BTC:2"]
    assert logs[-1]["outcome"] == "done"
    assert _partitions(tmp_path) == {("trivial", "BTC"): (Decimal("0"), Decimal("40"))}


class _ReadsPositionsOnce(VenueLink):
    """Only the first positions read answers. Every later one fails."""

    def __init__(self, venue: Exchange) -> None:
        super().__init__(venue)
        self._reads = 0

    async def fetch_positions(self) -> dict[str, Decimal] | VenueReadFailure:
        self._reads += 1
        if self._reads > 1:
            return VenueReadFailure.SEND_FAILED
        return await super().fetch_positions()


def test_a_venue_that_never_answers_after_a_close_gives_up_and_the_run_ends(
    tmp_path: Path,
) -> None:
    # Each failed read is a dry attempt, so a venue that cannot answer never hangs
    # the run (ADR-0060). It sends no second close.
    _hold(tmp_path / "saga.db", {"BTC": "0.02"})

    exit_code, logs = _flatten(tmp_path, link=_ReadsPositionsOnce)

    assert exit_code == 1
    dry = [log for log in logs if log["event"] == "flatten.dry_attempt"]
    assert [(log["count"], log["reason"]) for log in dry] == [
        (1, "read_failed"),
        (2, "read_failed"),
        (3, "read_failed"),
    ]
    # The close filled, but no read could say so. So the event names the last
    # size the venue reported, from before the close.
    gave_up = [log for log in logs if log["event"] == "flatten.gave_up"]
    assert [(log["symbol"], log["size"]) for log in gave_up] == [("BTC", "0.02")]
    assert not [log for log in logs if log.get("signal_id") == f"{OPERATOR}:BTC:2"]
    assert logs[-1]["outcome"] == "stopped"
    assert logs[-1]["left"] == f"read_failed: {VenueReadFailure.SEND_FAILED.value}"


def _left_open(path: Path, symbol: str, *, quantity: str = "0.02") -> str:
    """Leave an ``__operator__`` close ``LIVE`` in the store, one paper never lists.

    That is a close the venue acked and then dropped (#242) before a crash. The
    boot can only arm its ghost window, so the saga is still open when flatten
    starts. Returns its cloid.
    """
    cloid = derive_cloid(f"{OPERATOR}:{symbol}:1")
    clock = ManualClock()
    with SQLiteStore(path) as store:
        paper = _paper(InMemoryBus(), clock, store)
        checks = Checkpointer(spec=paper.account_spec(), store=store, clock=clock, specs=_SPECS)
        checks.recover()
        checks.checkpoint(
            Order(
                cloid=cloid,
                strategy_id=OPERATOR,
                signal_id=f"{OPERATOR}:{symbol}:1",
                symbol=symbol,
                side=Side.SELL,
                quantity=Decimal(quantity),
                order_type=OrderType.MARKET,
                state=OrderState.LIVE,
                created_ts_ns=0,
                # As the manager stores it at place, so a fill can be booked (ADR-0054).
                split_basis=checks.split_basis(symbol),
            )
        )
    return cloid


def test_flatten_waits_for_the_verdict_on_a_close_the_boot_left_open_before_it_closes_again(
    tmp_path: Path,
) -> None:
    # The dropped close could still have filled. A second close sent beside it
    # could close the size twice, so a symbol never has two closes open (ADR-0056).
    _hold(tmp_path / "saga.db", {"BTC": "0.02"})
    left_open = _left_open(tmp_path / "saga.db", "BTC")

    exit_code, logs = _flatten(tmp_path)

    assert exit_code == 0
    events = [(log["event"], log.get("cloid"), log.get("signal_id")) for log in logs]
    verdict = events.index(("ghost.reconciled", left_open, None))
    placed = events.index(("order.placed", None, f"{OPERATOR}:BTC:2"))
    assert verdict < placed
    assert _partitions(tmp_path) == {("trivial", "BTC"): (Decimal("0"), Decimal("40"))}


class _ReadsOnlyAtBoot(VenueLink):
    """The boot reads every order. After that, no order read reaches the venue."""

    def __init__(self, venue: Exchange) -> None:
        super().__init__(venue)
        self.reads = 0

    async def fetch_order(self, ref: OrderRef) -> VenueOrderView | VenueReadFailure:
        self.reads += 1
        if self.reads == 1:
            return await super().fetch_order(ref)
        return VenueReadFailure.SEND_FAILED


def test_a_close_left_open_that_no_pass_can_decide_gets_no_second_close_beside_it(
    tmp_path: Path,
) -> None:
    # Each failed pass is a dry attempt, as it is after a close (ADR-0060). After 3
    # the old close is still open. The run stops with the size left, and the next
    # boot resumes the saga.
    _hold(tmp_path / "saga.db", {"BTC": "0.02"})
    _left_open(tmp_path / "saga.db", "BTC")

    exit_code, logs = _flatten(tmp_path, link=_ReadsOnlyAtBoot)

    assert exit_code == 1
    dry = [log for log in logs if log["event"] == "flatten.dry_attempt"]
    assert [(log["count"], log["reason"]) for log in dry] == [
        (1, "read_failed"),
        (2, "read_failed"),
        (3, "read_failed"),
    ]
    assert [log["symbol"] for log in logs if log["event"] == "flatten.gave_up"] == ["BTC"]
    assert logs[-1]["left"] == "BTC=0.02"
    assert not [log for log in logs if log.get("signal_id") == f"{OPERATOR}:BTC:2"]


def test_a_failed_read_after_the_wait_is_a_dry_attempt_and_the_next_read_sizes_the_close(
    tmp_path: Path,
) -> None:
    # The old close may have filled during the wait. With no read of what is left,
    # flatten cannot size a close (ADR-0060). So it waits and reads again, as it
    # does after a close, and only that read sizes the next one.
    _hold(tmp_path / "saga.db", {"BTC": "0.02"})
    _left_open(tmp_path / "saga.db", "BTC")

    # The first positions read is flatten's own. The second is the read after the wait.
    exit_code, logs = _flatten(tmp_path, link=partial(_FailsOneRead, read="fetch_positions", nth=2))

    assert exit_code == 0
    dry = [log for log in logs if log["event"] == "flatten.dry_attempt"]
    assert [(log["count"], log["reason"]) for log in dry] == [(1, "read_failed")]
    events = [(log["event"], log.get("signal_id")) for log in logs]
    assert events.index(("flatten.dry_attempt", None)) < events.index(
        ("order.placed", f"{OPERATOR}:BTC:2")
    )
    assert logs[-1]["outcome"] == "done"
    assert _partitions(tmp_path) == {("trivial", "BTC"): (Decimal("0"), Decimal("40"))}


class _Venue:
    """Paper on a bus of its own, so it outlives the engine run it serves.

    The real venue keeps an order a crashed engine never booked. Paper keeps its
    book in memory only (#484), so the test holds one instance across both runs.
    """

    def __init__(self, path: Path, clock: ManualClock) -> None:
        self.bus = InMemoryBus()
        self.store = SQLiteStore(path)
        self._engine_bus: InMemoryBus | None = None
        self.paper = PaperExchange(
            bus=self.bus,
            clock=clock,
            fill_model=ImmediateFillModel(),
            genesis_collateral=GENESIS,
            instrument_specs=_SPECS,
            account_net=lambda: paper_store_reads(self.store).account_net(),
            applied_fills=lambda cloid: paper_store_reads(self.store).applied_fills(cloid),
        )
        self.bus.subscribe(ExecutionReport, self._report)

    def serve(self, engine_bus: InMemoryBus) -> None:
        """Link the venue to a new engine run: its ticks in, the venue's reports out."""
        self._engine_bus = engine_bus

        async def tick(tick: MarketTick) -> None:
            await self.bus.publish(tick)

        engine_bus.subscribe(MarketTick, tick)

    def cut(self) -> None:
        """The engine dies. Nothing the venue says from here reaches it."""
        self._engine_bus = None

    async def _report(self, report: ExecutionReport) -> None:
        if self._engine_bus is not None:
            await self._engine_bus.publish(report)


class _CrashesAfterTheSend(VenueLink):
    """The close reaches the venue, and the engine dies before it hears the fill."""

    def __init__(self, venue: Exchange, crash: Callable[[], None]) -> None:
        super().__init__(venue)
        self._crash = crash
        self.crashed = asyncio.Event()

    async def place(self, order: PlaceOrder) -> None:
        self._crash()
        await super().place(order)
        self.crashed.set()
        await asyncio.Event().wait()


def _engine(
    venue: _Venue, exchange: Exchange, tmp_path: Path, job: ExitJob | None = None
) -> Engine:
    """One engine run of ``job``, flatten by default, served by ``venue`` through ``exchange``.

    Each run replays the file from its first row, so each gets its own clock.
    """
    bus = InMemoryBus()
    clock = ManualClock()
    venue.serve(bus)
    return Engine(
        bus=bus,
        clock=clock,
        store=venue.store,
        exchange=exchange,
        feed=ReplayFeed(path=_ticks(tmp_path / "ticks.jsonl", _ROWS), bus=bus, clock=clock),
        guard=RealGuard(specs=_SPECS, store=venue.store, clock=clock),
        exit_job=Flatten() if job is None else job,
    )


def _crash_a_flatten(tmp_path: Path) -> _Venue:
    """Run flatten until its first close reaches paper, then kill the run.

    Returns the venue that still holds the fill, with the store open again.
    """
    venue = _Venue(tmp_path / "saga.db", ManualClock())
    link = _CrashesAfterTheSend(venue.paper, venue.cut)

    async def first_life() -> None:
        run = asyncio.create_task(_engine(venue, link, tmp_path).run())
        await asyncio.wait_for(link.crashed.wait(), timeout=5)
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)

    asyncio.run(first_life())
    venue.store.close()
    venue.store = SQLiteStore(tmp_path / "saga.db")
    return venue


def _next_run(
    venue: _Venue,
    tmp_path: Path,
    job: ExitJob,
    *,
    link: Callable[[PaperExchange], Exchange] | None = None,
) -> tuple[int, list[EventDict]]:
    """Run ``job`` after the crash. Returns the exit code and every event it emitted.

    ``link`` puts a venue double in front of paper.
    """
    exchange = venue.paper if link is None else link(venue.paper)

    async def go() -> int:
        return await asyncio.wait_for(_engine(venue, exchange, tmp_path, job).run(), 5)

    with capture_events() as logs:
        exit_code = asyncio.run(go())
    venue.store.close()
    return exit_code, logs


def test_a_flatten_that_crashed_after_its_send_is_booked_by_the_split_and_the_rest_closed(
    tmp_path: Path,
) -> None:
    # Two partitions share BTC, so the boot books the resumed fill by its stored
    # split (ADR-0054). The other symbol is still open, and the next run closes it.
    _hold(tmp_path / "saga.db", {"BTC": "0.02", "ETH": "0.5"})
    _hold(tmp_path / "saga.db", {"BTC": "0.01"}, strategy_id="other")
    venue = _crash_a_flatten(tmp_path)

    exit_code, logs = _next_run(venue, tmp_path, Flatten())

    assert exit_code == 0
    assert logs[-1]["outcome"] == "done"
    # Opened at 42000 and 3000. Closed at the last rows, 44000 and 3200 (ADR-0059).
    assert _partitions(tmp_path) == {
        ("trivial", "BTC"): (Decimal("0"), Decimal("40")),
        ("other", "BTC"): (Decimal("0"), Decimal("20")),
        ("trivial", "ETH"): (Decimal("0"), Decimal("100")),
    }
    with SQLiteStore(tmp_path / "saga.db") as store:
        closes = [order for order in store.all_orders() if order.strategy_id == OPERATOR]
    # One close per symbol, so no size was closed twice. The resumed run's seq is
    # above the crashed run's.
    assert sorted((order.symbol, order.signal_id) for order in closes) in (
        [("BTC", f"{OPERATOR}:BTC:1"), ("ETH", f"{OPERATOR}:ETH:2")],
        [("BTC", f"{OPERATOR}:BTC:2"), ("ETH", f"{OPERATOR}:ETH:1")],
    )
    assert all(order.state is OrderState.FILLED for order in closes)


def test_a_cancel_all_after_a_crashed_flatten_books_the_close_at_boot(tmp_path: Path) -> None:
    # The resume is the boot, not the flatten job, so any exit run does it (ADR-0056).
    _hold(tmp_path / "saga.db", {"BTC": "0.02"})
    _hold(tmp_path / "saga.db", {"BTC": "0.01"}, strategy_id="other")
    venue = _crash_a_flatten(tmp_path)

    exit_code, logs = _next_run(venue, tmp_path, OperatorCancelAll())

    assert exit_code == 0
    assert logs[-1]["job"] == "cancel_all"
    assert _partitions(tmp_path) == {
        ("trivial", "BTC"): (Decimal("0"), Decimal("40")),
        ("other", "BTC"): (Decimal("0"), Decimal("20")),
    }
    with SQLiteStore(tmp_path / "saga.db") as store:
        close = store.get_order(derive_cloid(f"{OPERATOR}:BTC:1"))
    assert close is not None
    assert close.state is OrderState.FILLED


class _FillsDuringTheWait(VenueLink):
    """``close`` is unknown to the boot's read. It fills just before the next read.

    So it fills after flatten's first position read and before the wait ends.
    """

    def __init__(self, venue: Exchange, *, close: PlaceOrder) -> None:
        super().__init__(venue)
        self._close = close
        self._reads = 0

    async def fetch_order(self, ref: OrderRef) -> VenueOrderView | VenueReadFailure:
        if ref.cloid == self._close.cloid:
            self._reads += 1
            if self._reads == 1:
                return VenueOrderView(status=None)
            if self._reads == 2:
                await self.place(self._close)
        return await super().fetch_order(ref)


def test_a_close_left_open_that_fills_during_the_wait_leaves_only_the_rest_to_close(
    tmp_path: Path,
) -> None:
    # A close sized from the read before the wait would ask for the whole size
    # again. Paper would shrink it to what is left and cancel the rest (ADR-0057).
    _hold(tmp_path / "saga.db", {"BTC": "0.02"})
    left_open = _left_open(tmp_path / "saga.db", "BTC", quantity="0.01")
    late = PlaceOrder(
        cloid=left_open,
        symbol="BTC",
        side=Side.SELL,
        quantity=Decimal("0.01"),
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.IOC,
        reduce_only=True,
    )
    venue = _Venue(tmp_path / "saga.db", ManualClock())

    exit_code, logs = _next_run(
        venue, tmp_path, Flatten(), link=partial(_FillsDuringTheWait, close=late)
    )

    assert exit_code == 0
    assert logs[-1]["outcome"] == "done"
    # Opened at 42000. Both halves close at the last row, 44000 (ADR-0059).
    assert _partitions(tmp_path) == {("trivial", "BTC"): (Decimal("0"), Decimal("40"))}
    with SQLiteStore(tmp_path / "saga.db") as store:
        old = store.get_order(left_open)
        close = store.get_order(derive_cloid(f"{OPERATOR}:BTC:2"))
    assert old is not None
    assert old.state is OrderState.FILLED
    assert close is not None
    assert close.quantity == Decimal("0.01")
    assert close.state is OrderState.FILLED
