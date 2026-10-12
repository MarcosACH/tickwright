"""Operator exit runs (ADR-0052, ADR-0060): one job in place of the strategies.

The ``Engine`` boots and reconciles as usual, then runs an ``ExitJob`` instead of
starting its strategies. The job gets what it needs in an ``ExitContext``,
because the engine builds those parts itself and the composition root never
sees them.
"""

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
            for symbol, size in positions.items():
                # No venue can send a market close without a price. The symbol
                # stays open, and the final read reports its size.
                if symbol not in priced:
                    continue
                resumed = await _resume_closes(symbol, size, context)
                if isinstance(resumed, VenueReadFailure):
                    return ExitOutcome(status=ExitStatus.STOPPED, left=describe_failure(resumed))
                if resumed:
                    await _await_verdict(await _close(symbol, resumed, emitter), context)
        return await _final_read(context)


async def _resume_closes(
    symbol: str, size: Decimal, context: ExitContext
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
        await _await_verdict(order.cloid, context)
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
