"""Operator exit runs (ADR-0052, ADR-0060): one job in place of the strategies.

The ``Engine`` boots and reconciles as usual, then runs an ``ExitJob`` instead of
starting its strategies. The job gets what it needs in an ``ExitContext``,
because the engine builds those parts itself and the composition root never
sees them.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from tickwright.domain import (
    AccountAnchor,
    InvariantViolation,
    OrderAnchor,
    OrderRef,
    VenueOpenOrder,
    VenueReadFailure,
)
from tickwright.observability import NamedEvent, named_event

from .checkpoint import Checkpointer


class ExitStatus(StrEnum):
    """How a job ended. Only a final venue read can make it ``DONE`` (ADR-0060)."""

    DONE = "done"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True, kw_only=True)
class ExitOutcome:
    """What a job reports back to the engine when it returns."""

    status: ExitStatus
    left: list[VenueOpenOrder] | VenueReadFailure
    """What the last venue read left, or how that read failed."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ExitContext:
    """The engine's own parts that a job may use, built after the barrier."""

    account: AccountAnchor
    orders: OrderAnchor
    checkpointer: Checkpointer


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
            return ExitOutcome(status=ExitStatus.STOPPED, left=left)
        if left:
            named_event(NamedEvent.CANCEL_ALL_ORDERS_REMAIN, orders=describe_orders(left))
            return ExitOutcome(status=ExitStatus.STOPPED, left=left)
        return ExitOutcome(status=ExitStatus.DONE, left=left)


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
