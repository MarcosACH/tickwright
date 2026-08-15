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
from collections.abc import Callable, Sequence
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple

from ledgers import GENESIS

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.feed import ReplayFeed
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    Account,
    ComponentState,
    FundingAccrual,
    InstrumentSpec,
    InvariantViolation,
    Order,
    OrderEvent,
    OrderFilled,
    Portfolio,
    Position,
    Side,
    Store,
    account_net_size,
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

_SLOTS = 50
"""Event-loop slots given to a life with no fill to wait on.

A count rather than a sleep, because nothing here may sleep: the replay task and
the funding generator interleave over a handful of turns, and this is simply
more than either needs. It bounds a *quiescence*, never a duration."""

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


def _write_rows(path: Path, rows: list[dict[str, str | int]]) -> Path:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return path


def _write_ticks(path: Path) -> Path:
    return _write_rows(path, _ROWS)


class _FundingRefusingStore(SQLiteStore):
    """The real store, refusing exactly the ledger write that carries a mark.

    Narrowed to the funding write so the opening fill still lands: what is under
    test is a refusal of *this* write, against a ledger that was healthy up to
    it, rather than a store that was never usable. Same narrowing, and the same
    reason, as the ``Checkpointer``'s own unit case
    (``tests/engine/test_checkpoint.py``); what this suite adds is the *whole
    pipeline's* answer to that refusal rather than the verb's.
    """

    def checkpoint_ledger(
        self,
        *,
        account: Account,
        positions: Sequence[Position] = (),
        order: Order | None = None,
        funding_mark: tuple[str, int] | None = None,
        ts_ns: int,
    ) -> None:
        if funding_mark is not None:
            raise InvariantViolation("disk is full")
        super().checkpoint_ledger(account=account, positions=positions, order=order, ts_ns=ts_ns)


class Run(NamedTuple):
    """One finished life, and the three ways its cases read it back.

    ``portfolio`` is the scoped facade the engine lent the strategy — the seam
    the whole surface exists to serve, and still readable after the store closes.
    ``accruals`` is every accrual the bus carried, which is how a case asks
    whether the generator was still alive. The durable half is read separately,
    by reopening the file.

    ``exit_code`` is ``None`` when the life was **still running** as the driver
    ran out of slots — reachable only under ``ask_to_stop=False``, where the
    question is precisely whether anything inside the engine ended it. A driver
    that awaited the run instead would hang there forever rather than fail.
    """

    exit_code: int | None
    state: ComponentState
    portfolio: Portfolio
    accruals: list[FundingAccrual]


async def _run(
    ticks: Path,
    db: Path,
    *,
    advance_after_stop: int | None = None,
    trade: bool = True,
    start_ns: int = 0,
    store_factory: Callable[[Path], Store] = SQLiteStore,
    ask_to_stop: bool = True,
) -> Run:
    """One supervised life: buy on the first tick, jump the boundaries, stop.

    ``advance_after_stop`` drives virtual time further **once the run has
    returned and while the event loop is still turning** — which is the only
    place that question can be asked. Advancing a clock after ``asyncio.run``
    has closed the loop would find nothing to run either way, so a case built
    that way would pass against a generator that was very much alive.

    ``trade=False`` registers no strategy, which is what a **second** life looks
    like when the book it holds was opened by the first: the position comes back
    from the ledger, not from a strategy re-placing anything, and the run has
    nothing to wait for but the clock. ``start_ns`` opens that life's clock
    where the previous one stopped, since the generator resumes from the startup
    instant and never from the watermark (ADR-0043 §5.1).

    ``ask_to_stop=False`` withholds the stop request, which is the only way to
    ask whether the engine ended the life **itself**. Every other case here asks
    for the stop, so a fault they observed would be one the ask produced; this
    one observes a run that had nobody to ask it. ``store_factory`` is what gives
    it something to fault *on*.
    """
    bus = InMemoryBus()
    clock = ManualClock(start_ns=start_ns)
    store = store_factory(db)
    exchange = PaperExchange(
        bus=bus,
        clock=clock,
        fill_model=ImmediateFillModel(),
        genesis_collateral=GENESIS,
        instrument_specs={"BTC": _SPEC},
        funding_interval_ns=_INTERVAL_NS,
        # The production wiring verbatim (``app/build.py``), because the thing
        # under test across two lives is precisely that the venue's notional
        # basis is the ledger's and not a tally of its own.
        account_net=lambda: account_net_size(store.all_positions()),
    )
    feed = ReplayFeed(path=ticks, bus=bus, clock=clock)
    engine = Engine(bus=bus, clock=clock, store=store, exchange=exchange, feed=feed)
    if trade:
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
    accruals: list[FundingAccrual] = []

    async def on_order_event(event: OrderEvent) -> None:
        if isinstance(event, OrderFilled):
            filled.set()

    async def on_accrual(accrual: FundingAccrual) -> None:
        accruals.append(accrual)

    bus.subscribe(OrderEvent, on_order_event)
    bus.subscribe(FundingAccrual, on_accrual)

    run = asyncio.create_task(engine.run())
    if trade:
        await asyncio.wait_for(filled.wait(), timeout=5)
    else:
        # Nothing to wait *for* but the file: replay runs to end-of-file on its
        # own, and the boundaries this life crosses are carried by its rows.
        for _ in range(_SLOTS):
            await asyncio.sleep(0)
    # Replay runs to end-of-file, which is what carries virtual time across the
    # boundaries; the engine keeps running after it, so the stop is ours to ask.
    await asyncio.sleep(0)
    exit_code: int | None
    if ask_to_stop:
        await engine.stop()
        exit_code = await run
        # Asserted here rather than left to each case: a graceful life that
        # faulted still re-walks the whole teardown membership, so every durable
        # number a case reopens the file to read was already written and would
        # come back identical. A case that reads only those numbers would pass on
        # a run this branch is meant to have stopped cleanly.
        assert engine.state is ComponentState.STOPPED
    else:
        # Nobody is going to end this life, so the only thing to wait on is the
        # engine ending it. Slots rather than a timeout, for the reason the whole
        # suite has: a wall-clock wait here would be a wait for nothing on the
        # path where the engine never faults, and every case must stay fast.
        for _ in range(_SLOTS):
            if run.done():
                break
            await asyncio.sleep(0)
        exit_code = await run if run.done() else None
        if exit_code is None:
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
    if advance_after_stop is not None:
        clock.advance_to(advance_after_stop)
        for _ in range(5):  # every slot a surviving generator would need
            await asyncio.sleep(0)
    return Run(exit_code, engine.state, engine.portfolio_for("trivial"), accruals)


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
    finished = asyncio.run(_run(_write_ticks(tmp_path / "ticks.jsonl"), tmp_path / "saga.db"))

    view = finished.portfolio.position("BTC")
    assert view is not None
    assert view.funding == Decimal("-6.3")  # 3 x -(0.5 x 42000 x 0.0001)
    assert view.entry_price == Decimal("42000")  # funding reaches no basis
    assert view.realized_pnl == Decimal("0")  # nor any PnL
    assert finished.portfolio.account().cash == GENESIS - Decimal("6.3")
    assert finished.exit_code == 0


def test_the_generator_is_dead_once_the_engine_has_stopped(tmp_path: Path) -> None:
    """The reverse shutdown's whole point at this seam, asserted by outliving it.

    ``exchange.stop`` sits ahead of the bus drain (ADR-0024) precisely because a
    generator that outlived the release would keep publishing into that drain and
    keep raising its high-water mark, so the cascade would never quiesce and a
    graceful stop would never finish. The exit code says the stop finished; this
    says why it could — driving virtual time across three *more* boundaries after
    the run returns produces nothing, because the loop is cancelled rather than
    merely unobserved.

    A stopped engine is what a cancelled task looks like from outside: there is
    no seam that reports a loop's liveness, so the observation is what it would
    have published.
    """
    finished = asyncio.run(
        _run(
            _write_ticks(tmp_path / "ticks.jsonl"),
            tmp_path / "saga.db",
            advance_after_stop=7_000,  # past 4 000, 5 000, 6 000 and 7 000
        )
    )

    # Only the three the run itself crossed; the four after the stop settled none.
    assert [accrual.boundary_ts_ns for accrual in finished.accruals] == [1_000, 2_000, 3_000]
    assert finished.exit_code == 0
    assert finished.state is ComponentState.STOPPED


def test_a_refused_funding_write_faults_the_engine_at_the_refusal(tmp_path: Path) -> None:
    """The containment ADR-0024 gives every engine-internal handler, at this one.

    Funding reaches the ledger through the bus rather than on a fill, so its
    publisher is the paper venue's boundary generator. That generator is a
    **supervised** task — ``Exchange.run()``, task-created in the runner's
    ``TaskGroup`` beside the feed — which is the whole of what makes this
    refusal behave like the fill path's: ``InMemoryBus.publish`` re-raises the
    handler's ``InvariantViolation`` into the generator, the ``TaskGroup``
    cancels its siblings, and the engine is ``FAULTED`` with a non-zero exit for
    the supervisor to restart on (ADR-0043 §4: the store is the sole authority
    on the paper path, so a write it refuses is not survivable).

    **Nobody asks this life to stop**, and that is the assertion rather than an
    incidental detail. A generator spawned outside the group dies alone: the
    engine stays ``RUNNING``, accrues nothing for the rest of the process, and
    the refusal surfaces only once a teardown someone else asked for reaches
    ``exchange.stop`` — loud, but arbitrarily late, and never at all in a run
    that is left alone. Asking for the stop would produce the non-zero exit
    either way and prove nothing about *when*.

    The one accrual on the bus is the boundary whose write was refused: the
    collector is subscribed ahead of the engine's own handler, so it sees the
    event that then fails. 2 000 and 3 000 never happen — which is the money
    line the fault exists to stop the process over.
    """
    finished = asyncio.run(
        _run(
            _write_ticks(tmp_path / "ticks.jsonl"),
            tmp_path / "saga.db",
            store_factory=_FundingRefusingStore,
            ask_to_stop=False,
        )
    )

    assert finished.exit_code == 1
    assert finished.state is ComponentState.FAULTED
    assert [accrual.boundary_ts_ns for accrual in finished.accruals] == [1_000]


def test_a_position_recovered_from_the_ledger_keeps_accruing(tmp_path: Path) -> None:
    """A restart is not a reason to stop charging funding on a position still open.

    The second life places nothing: its book is the one the **ledger** handed
    back, which is the whole of what ADR-0043 §6 recovery is for. A generator
    whose notional basis were the venue's own in-process memory would find that
    memory empty and settle nothing — for this life and every one after it,
    since only a new fill would ever refill it. So the position would sit open
    and free of funding indefinitely, and the ledger would record a long that
    never paid.

    That is a different claim from the restart **gap**, which stands unchanged:
    boundaries that elapsed while the process was down are still skipped, the
    loop resuming from the startup instant (ADR-0043 §5.1). What is asserted
    here is that the boundaries *after* it are charged — 5 000, 6 000 and 7 000,
    against the 0.5 BTC the first life left open — at the price cached when each
    matured, a matured cadence firing before the row that carried time past it
    (ADR-0033).
    """
    db = tmp_path / "saga.db"
    asyncio.run(_run(_write_ticks(tmp_path / "first.jsonl"), db))

    second = asyncio.run(
        _run(
            _write_rows(
                tmp_path / "second.jsonl",
                [
                    {
                        "symbol": "BTC",
                        "price": "42000",
                        "size": "3",
                        "aggressor_side": "buy",
                        "trade_id": "c",
                        "ts_event": 4_100,
                    },
                    # Past 5 000, 6 000 and 7 000, exactly as the first life's
                    # second row jumped past its own three.
                    {
                        "symbol": "BTC",
                        "price": "42200",
                        "size": "3",
                        "aggressor_side": "sell",
                        "trade_id": "d",
                        "ts_event": 7_500,
                    },
                ],
            ),
            db,
            trade=False,
            start_ns=4_000,
        )
    )

    assert [accrual.boundary_ts_ns for accrual in second.accruals] == [5_000, 6_000, 7_000]
    assert [accrual.amount for accrual in second.accruals] == [Decimal("-2.1")] * 3
    # Six boundaries charged across the two lives, on one unbroken 0.5 long.
    store = SQLiteStore(db)
    assert [position.funding for position in store.all_positions()] == [Decimal("-12.6")]
    account = store.load_account()
    assert account is not None
    assert account.cash == GENESIS - Decimal("12.6")
    assert second.exit_code == 0


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
