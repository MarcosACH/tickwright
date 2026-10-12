"""Operator exit runs (ADR-0052, ADR-0060): one job in place of the strategies.

The ``Engine`` boots and reconciles as usual, then runs an ``ExitJob`` instead of
starting its strategies. The job gets what it needs in an ``ExitContext``,
because the engine builds those parts itself and the composition root never
sees them.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from tickwright.domain import (
    OPERATOR,
    AccountAnchor,
    Clock,
    EventBus,
    InvariantViolation,
    MarketTick,
    OrderAnchor,
    OrderRef,
    OrderType,
    PreTradeGuard,
    Side,
    SignalEmitter,
    TimeInForce,
    VenueOpenOrder,
    VenueReadFailure,
    derive_cloid,
)
from tickwright.observability import NamedEvent, named_event

from .checkpoint import Checkpointer
from .feed_handle import FeedHandle
from .reconcile import Reconciler


class ExitStatus(StrEnum):
    """How a job ended. Only a final venue read can make it ``DONE`` (ADR-0060)."""

    DONE = "done"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True, kw_only=True)
class ExitOutcome:
    """What a job reports back to the engine when it returns."""

    status: ExitStatus
    left: str
    """What the last venue read left, or how that read failed, for ``exit.finished``.

    The job writes it, because only the job knows what its last read covered.
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class ExitContext:
    """The engine's own parts that a job may use, built after the barrier."""

    account: AccountAnchor
    orders: OrderAnchor
    checkpointer: Checkpointer
    guard: PreTradeGuard
    bus: EventBus
    clock: Clock
    feed: FeedHandle
    reconciler: Reconciler
    pass_interval_seconds: float
    """How long a job waits between its own reconcile passes. No cadence runs in an
    exit run, so the job paces them itself (ADR-0052)."""


class ExitJob(Protocol):
    """One operator job the engine runs in place of its strategies."""

    @property
    def name(self) -> str:
        """The job's name in ``exit.finished``."""
        ...

    async def run(self, context: ExitContext) -> ExitOutcome:
        """Do the job and say what the venue holds at the end."""
        ...


class OperatorCancelAll:
    """``tickwright cancel-all``: cancel every order resting in the account."""

    name = "cancel_all"

    async def run(self, context: ExitContext) -> ExitOutcome:
        left = await context.account.fetch_open_orders()
        # One cancel, then one more for whatever the read still shows (ADR-0060).
        for _ in range(2):
            left = await _cancel_round(left, context)
        if isinstance(left, VenueReadFailure):
            return ExitOutcome(status=ExitStatus.STOPPED, left=describe_failure(left))
        if left:
            named_event(NamedEvent.CANCEL_ALL_ORDERS_REMAIN, orders=describe_orders(left))
            return ExitOutcome(status=ExitStatus.STOPPED, left=describe_orders(left))
        return ExitOutcome(status=ExitStatus.DONE, left=NOTHING_LEFT)


class Flatten:
    """``tickwright flatten``: close every position the account holds."""

    name = "flatten"

    async def run(self, context: ExitContext) -> ExitOutcome:
        # First, so a crash mid-flatten still leaves the store halted (ADR-0053).
        context.guard.trip_kill_switch("flatten: operator exit")
        # A guard with no kill switch reads untripped right after the trip.
        if not context.guard.kill_switch_tripped:
            named_event(
                NamedEvent.EXIT_UNGUARDED,
                warning="no kill switch under GUARD=noop: the engine trades as soon as it restarts",
            )
        # A resting order could fill after the close and reopen exposure. So the
        # cancel all step runs first, and flatten stops where it stops (ADR-0060).
        cancelled = await OperatorCancelAll().run(context)
        if cancelled.status is ExitStatus.STOPPED:
            return cancelled
        positions = await context.account.fetch_positions()
        if isinstance(positions, VenueReadFailure):
            return ExitOutcome(status=ExitStatus.STOPPED, left=describe_failure(positions))
        if positions:
            priced: set[str] = set()

            async def note_price(tick: MarketTick) -> None:
                priced.add(tick.symbol)

            # Before the feed starts, so no tick goes unseen.
            context.bus.subscribe(MarketTick, note_price)
            await context.feed.start(positions)
            # Only a replay ends, and it plays the whole file first. So each close
            # fills at the last row (ADR-0059). The CLI refuses a live feed until #475.
            await context.feed.ended()
            emitter = SignalEmitter(strategy_id=OPERATOR, bus=context.bus, clock=context.clock)
            # Seeded from the sagas, as a strategy's is. An earlier flatten's seq
            # is consumed, and the manager drops a signal id it has seen (ADR-0006).
            high_water = context.checkpointer.cache.seq_high_water()
            emitter.set_next_seq(high_water.get(OPERATOR, 0) + 1)
            passes = _Passes(context.reconciler)
            # One loop per symbol, all at the same time, so a thin book on one
            # symbol does not hold back the others (ADR-0056). A task group
            # cancels the rest when one faults.
            async with asyncio.TaskGroup() as group:
                loops = [
                    group.create_task(
                        _flatten_symbol(symbol, size, priced, emitter, passes, context)
                    )
                    for symbol, size in positions.items()
                ]
            for loop in loops:
                if (failure := loop.result()) is not None:
                    return ExitOutcome(status=ExitStatus.STOPPED, left=describe_failure(failure))
        return await _final_read(context)


DRY_ATTEMPTS_TO_GIVE_UP = 3
"""Dry attempts in a row that stop a symbol's loop (ADR-0056)."""

DRY_WAIT_SECONDS = 2.0
"""The wait after a dry attempt, before the next one (ADR-0056)."""


class _Passes:
    """One reconcile pass at a time, shared by every loop of one flatten run.

    A loop that needs a pass waits for the one in progress, then runs its own.
    A pass that started before the loop's attempt ended may not cover it.
    """

    def __init__(self, reconciler: Reconciler) -> None:
        self._reconciler = reconciler
        self._lock = asyncio.Lock()

    async def run(self) -> bool:
        """Run both halves of one pass. False when either failed."""
        async with self._lock:
            return (
                await self._reconciler.reconcile_inflight()
                and await self._reconciler.reconcile_open_orders()
            )


async def _flatten_symbol(
    symbol: str,
    size: Decimal,
    priced: set[str],
    emitter: SignalEmitter,
    passes: _Passes,
    context: ExitContext,
) -> VenueReadFailure | None:
    """One symbol's loop: wait for a close the boot left open, then close the rest."""
    resumed = await _resume_closes(symbol, size, passes, context)
    if isinstance(resumed, VenueReadFailure):
        return resumed
    if resumed:
        await _close_symbol(symbol, resumed, priced, emitter, passes, context)
    return None


async def _close_symbol(
    symbol: str,
    size: Decimal,
    priced: set[str],
    emitter: SignalEmitter,
    passes: _Passes,
    context: ExitContext,
) -> None:
    """Close ``symbol`` until the venue reads it flat, or stop after 3 dry attempts in a row.

    A fill resets the count, so a thin book that keeps filling a little never gives up
    (ADR-0056). A failed read is dry too, so a venue that cannot answer never hangs
    the run (ADR-0060). The final read reports what is left.
    """
    dry = _DryAttempts(symbol, context)
    while True:
        # No venue can send a market close without a price.
        if symbol not in priced:
            reason = "no_price"
        elif await _size_gap(symbol, size, context):
            reason = "size_gap"
        else:
            cloid = await _close(symbol, size, emitter)
            # A second close beside an open one could close the size twice. So a failed
            # pass waits for this close again, and a give-up leaves it to the next boot.
            while not await _await_verdict(cloid, passes, context):
                if await dry.gave_up("read_failed", size):
                    return
            read = await _read_size(symbol, size, dry, context)
            # None is a give-up on the reads. Zero is a flat venue.
            if not read:
                return
            size = read
            if _filled(cloid, context):
                dry.reset()
                continue
            reason = "no_fill"
        if await dry.gave_up(reason, size):
            return
        if reason == "size_gap":
            # The gap can be the venue's side too, so the next check needs a new read.
            read = await _read_size(symbol, size, dry, context)
            if not read:
                return
            size = read


class _DryAttempts:
    """One symbol's count of attempts in a row that closed nothing (ADR-0056)."""

    def __init__(self, symbol: str, context: ExitContext) -> None:
        self._symbol = symbol
        self._context = context
        self._count = 0

    def reset(self) -> None:
        self._count = 0

    async def gave_up(self, reason: str, size: Decimal) -> bool:
        """Count one dry attempt. Say whether it was the last, or wait before the next."""
        self._count += 1
        named_event(
            NamedEvent.FLATTEN_DRY_ATTEMPT, symbol=self._symbol, count=self._count, reason=reason
        )
        if self._count == DRY_ATTEMPTS_TO_GIVE_UP:
            named_event(NamedEvent.FLATTEN_GAVE_UP, symbol=self._symbol, size=str(size))
            return True
        await self._context.clock.sleep(DRY_WAIT_SECONDS)
        return False


async def _size_gap(symbol: str, size: Decimal, context: ExitContext) -> bool:
    """Whether the partitions on ``symbol`` fail to sum to the venue's ``size``.

    A close is split across the partitions it closes (ADR-0054). Against a gap,
    some of it would land on no partition, or one would be left holding size.
    The gap can be fills the bus has not delivered yet, so a drain comes first.
    """
    if _held(symbol, context) == size:
        return False
    await context.bus.drain()
    return _held(symbol, context) != size


def _held(symbol: str, context: ExitContext) -> Decimal:
    """The sum of every partition's size on ``symbol``."""
    return sum((part for _owner, part in context.checkpointer.split_basis(symbol)), Decimal(0))


async def _read_size(
    symbol: str, held: Decimal, dry: _DryAttempts, context: ExitContext
) -> Decimal | None:
    """The venue's size on ``symbol``, read again after each failed read.

    ``None`` means the failed reads used up the dry attempts. ``held`` is the
    last size read, for ``flatten.gave_up``.

    Zero is a flat venue. A hand trade the books cannot see may have closed it,
    and no order can close what the venue does not hold. So what the strategies
    still hold moves to the unattributed partition first (ADR-0054, ADR-0056).
    """
    while isinstance(positions := await context.account.fetch_positions(), VenueReadFailure):
        if await dry.gave_up("read_failed", held):
            return None
    size = positions.get(symbol, Decimal(0))
    if not size:
        context.checkpointer.checkpoint_leftover(symbol)
    return size


async def _resume_closes(
    symbol: str, size: Decimal, passes: _Passes, context: ExitContext
) -> Decimal | VenueReadFailure:
    """Wait for any close the boot left open in ``symbol``, then say what is left to close.

    That close could still fill, so a second one sent beside it could close the
    size twice. A symbol never has two closes open (ADR-0056). So a wait that ends
    with one still open leaves nothing to close. The final read reports the size,
    and the next boot resumes the saga. The wait runs after the feed has played,
    because its passes move the replay clock.
    """
    left_open = context.checkpointer.cache.open_orders(strategy_id=OPERATOR, symbol=symbol)
    if not left_open:
        return size
    for order in left_open:
        await _await_verdict(order.cloid, passes, context)
    if not all(_decided(order.cloid, context) for order in left_open):
        return Decimal(0)
    positions = await context.account.fetch_positions()
    if isinstance(positions, VenueReadFailure):
        return positions
    return positions.get(symbol, Decimal(0))


async def _close(symbol: str, size: Decimal, emitter: SignalEmitter) -> str:
    """Send one reduce-only market IOC order for the whole of ``size`` (ADR-0054).

    Returns the cloid of the order's saga.
    """
    signal_id = await emitter.place(
        symbol=symbol,
        side=Side.SELL if size > 0 else Side.BUY,
        quantity=abs(size),
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.IOC,
        reduce_only=True,
    )
    return derive_cloid(signal_id)


async def _await_verdict(cloid: str, passes: _Passes, context: ExitContext) -> bool:
    """Run reconcile passes until the close's saga is terminal (ADR-0011, ADR-0026).

    A pass covers both halves. ``reconcile_inflight`` resolves a send that never
    acked. ``reconcile_open_orders`` resolves an acked order whose last push was lost.

    A failed pass ends the wait and returns False, so a venue that cannot answer
    never hangs the run. The saga stays open, and the caller decides what is next.
    """
    while not _decided(cloid, context):
        await context.clock.sleep(context.pass_interval_seconds)
        if not await passes.run():
            return False
    return True


def _decided(cloid: str, context: ExitContext) -> bool:
    saga = context.checkpointer.cache.get_order(cloid)
    # The manager writes a saga for every signal it handles, a denial included.
    return saga is None or saga.is_terminal


def _filled(cloid: str, context: ExitContext) -> bool:
    saga = context.checkpointer.cache.get_order(cloid)
    return saga is not None and saga.cum_qty > 0


async def _final_read(context: ExitContext) -> ExitOutcome:
    """Done only when the venue holds no resting order and no position (ADR-0060)."""
    orders = await context.account.fetch_open_orders()
    if isinstance(orders, VenueReadFailure):
        return ExitOutcome(status=ExitStatus.STOPPED, left=describe_failure(orders))
    positions = await context.account.fetch_positions()
    if isinstance(positions, VenueReadFailure):
        return ExitOutcome(status=ExitStatus.STOPPED, left=describe_failure(positions))
    if orders or positions:
        left = [describe_orders(orders)] if orders else []
        left += [f"{symbol}={size}" for symbol, size in positions.items()]
        return ExitOutcome(status=ExitStatus.STOPPED, left=",".join(left))
    return ExitOutcome(status=ExitStatus.DONE, left=NOTHING_LEFT)


NOTHING_LEFT = "none"
"""``left`` when the last read found nothing to act on."""


def describe_failure(failure: VenueReadFailure) -> str:
    """``left`` when the last read failed."""
    return f"read_failed: {failure.value}"


def describe_orders(orders: Sequence[VenueOpenOrder]) -> str:
    """Name each order by its oid, or by its cloid where the venue gives no oid.

    One string, because every catalog field is a scalar.
    """
    return ",".join(order.venue_oid or order.cloid or "?" for order in orders)


async def _cancel_round(
    resting: list[VenueOpenOrder] | VenueReadFailure, context: ExitContext
) -> list[VenueOpenOrder] | VenueReadFailure:
    """Cancel what ``resting`` shows, then read again.

    A failed read or an empty book is returned as it is, with nothing sent. The
    cancel statuses are never read. Only the next read says what is left.
    """
    if isinstance(resting, VenueReadFailure) or not resting:
        return resting
    await context.orders.cancel([_mark(order, context.checkpointer) for order in resting])
    return await context.account.fetch_open_orders()


def _mark(order: VenueOpenOrder, checkpointer: Checkpointer) -> OrderRef:
    """The ref to cancel ``order`` by, with its saga marked first if it has one.

    The mark is durable before the send. Without it, a crash after the send
    leaves reconcile to judge the order a ghost and mark it ``REJECTED``.
    """
    if order.cloid is None:
        # Only a live venue lists an order the engine did not place (#470).
        raise InvariantViolation(f"cannot cancel an order with no cloid on {order.symbol}")
    saga = checkpointer.cache.get_order(order.cloid)
    if saga is None:
        return OrderRef(cloid=order.cloid, symbol=order.symbol, venue_oid=order.venue_oid)
    saga.mark_operator_cancel(ts_ns=checkpointer.clock.timestamp_ns())
    checkpointer.checkpoint(saga)
    return OrderRef.of(saga)
