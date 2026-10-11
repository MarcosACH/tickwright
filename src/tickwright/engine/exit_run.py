"""Operator exit runs (ADR-0052, ADR-0060): one job in place of the strategies.

The ``Engine`` boots and reconciles as usual, then runs an ``ExitJob`` instead of
starting its strategies. The job gets what it needs in an ``ExitContext``,
because the engine builds those parts itself and the composition root never
sees them.
"""

import asyncio
from collections.abc import Collection, Sequence
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
    MarketFeed,
    MarketTick,
    OrderAnchor,
    OrderRef,
    OrderType,
    PlaceSignal,
    PreTradeGuard,
    Side,
    TimeInForce,
    VenueOpenOrder,
    VenueReadFailure,
    derive_cloid,
)
from tickwright.observability import NamedEvent, named_event

from .checkpoint import Checkpointer
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
    feed: "FeedHandle"
    reconciler: Reconciler
    pass_interval_seconds: float
    """How long a job waits between its own reconcile passes. No cadence runs in an
    exit run, so the job paces them itself (ADR-0052)."""


class FeedHandle:
    """The one way the engine starts the feed.

    A normal run starts it with no symbols to add. An exit job starts it for the
    symbols it needs. The engine builds it over its own task group, so the feed
    loop is supervised the same way on both paths.
    """

    def __init__(self, feed: MarketFeed, tasks: asyncio.TaskGroup) -> None:
        self._feed = feed
        self._tasks = tasks
        self.task: asyncio.Task[None] | None = None
        """The feed loop, for the engine's teardown. ``None`` until started."""

    async def start(self, symbols: Collection[str] = ()) -> None:
        """Add ``symbols`` to the feed, connect it, and run its loop."""
        # A feed that cannot add symbols raises on any call, as Hyperliquid does
        # until #475. A normal run adds none, so it must not call at all.
        if symbols:
            self._feed.add_symbols(symbols)
        await self._feed.start()
        named_event(NamedEvent.ENGINE_FEED_STARTED)
        self.task = self._tasks.create_task(self._feed.run())

    async def ended(self) -> None:
        """Wait for the feed loop to return. A feed that fails faults the run instead."""
        if self.task is not None:
            await asyncio.wait([self.task])


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
            for symbol, size in positions.items():
                # No venue can send a market close without a price. The symbol
                # stays open, and the final read reports its size.
                if symbol in priced:
                    await _await_verdict(await _close(symbol, size, context), context)
        return await _final_read(context)


async def _close(symbol: str, size: Decimal, context: ExitContext) -> str:
    """Send one reduce-only market IOC order for the whole of ``size`` (ADR-0054).

    Returns the cloid of the order's saga.
    """
    now = context.clock.timestamp_ns()
    signal = PlaceSignal(
        ts_event=now,
        ts_init=now,
        strategy_id=OPERATOR,
        symbol=symbol,
        seq=1,
        side=Side.SELL if size > 0 else Side.BUY,
        quantity=abs(size),
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.IOC,
        reduce_only=True,
    )
    await context.bus.publish(signal)
    return derive_cloid(signal.signal_id)


async def _await_verdict(cloid: str, context: ExitContext) -> None:
    """Run reconcile passes until the close's saga is terminal (ADR-0011, ADR-0026).

    A pass covers both halves. ``reconcile_inflight`` resolves a send that never
    acked. ``reconcile_open_orders`` resolves an acked order whose last push was lost.

    A failed pass ends the wait, so a venue that cannot answer never hangs the run.
    The saga stays open, and the next boot resumes it.
    """
    while not _decided(cloid, context):
        await context.clock.sleep(context.pass_interval_seconds)
        if not await context.reconciler.reconcile_inflight():
            return
        if not await context.reconciler.reconcile_open_orders():
            return


def _decided(cloid: str, context: ExitContext) -> bool:
    saga = context.checkpointer.cache.get_order(cloid)
    # The manager writes a saga for every signal it handles, a denial included.
    return saga is None or saga.is_terminal


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
