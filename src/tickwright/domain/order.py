"""The order-lifecycle saga record (ADR-0007 / ADR-0008).

``Order.apply`` concentrates the crash-safety correctness argument into one
unit-testable place with zero infrastructure: it is the sole authority for
transition legality and for dedup. It is idempotent — a no-op on an event whose
``event_id`` is already reflected, so at-least-once redelivery converges — and it
raises ``InvariantViolation`` on an illegal transition rather than corrupting
state (fail-fast, ADR-0014).

The transition table is the full 9-state FSM: the happy path through ``LIVE``
and ``PARTIALLY_FILLED``, the terminal taxonomy ``DENIED``/``REJECTED``/``FAILED``
(ADR-0010), cancels, and the ghost-reconciliation resolutions.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal

from .enums import OrderState, OrderType, Side
from .errors import InvariantViolation
from .events import (
    OrderDenied,
    OrderEvent,
    OrderFailed,
    OrderFilled,
    OrderFillEvent,
    OrderPartiallyFilled,
    OrderRejected,
)

# Legal saga transitions as (from_state, to_state) pairs (ADR-0007).
_LEGAL_TRANSITIONS: frozenset[tuple[OrderState, OrderState]] = frozenset(
    {
        (OrderState.PENDING, OrderState.SUBMITTED),
        (OrderState.PENDING, OrderState.DENIED),
        (OrderState.SUBMITTED, OrderState.LIVE),
        (OrderState.SUBMITTED, OrderState.PARTIALLY_FILLED),
        (OrderState.SUBMITTED, OrderState.FILLED),
        (OrderState.SUBMITTED, OrderState.CANCELLED),
        (OrderState.SUBMITTED, OrderState.REJECTED),
        (OrderState.SUBMITTED, OrderState.FAILED),
        (OrderState.LIVE, OrderState.PARTIALLY_FILLED),
        (OrderState.LIVE, OrderState.FILLED),
        (OrderState.LIVE, OrderState.CANCELLED),
        (OrderState.LIVE, OrderState.REJECTED),
        (OrderState.PARTIALLY_FILLED, OrderState.PARTIALLY_FILLED),
        (OrderState.PARTIALLY_FILLED, OrderState.FILLED),
        (OrderState.PARTIALLY_FILLED, OrderState.CANCELLED),
    }
)

# The terminal states: no transition leaves them, so a cancel request against
# one is a no-op (ADR-0010 taxonomy, ADR-0026).
_TERMINAL_STATES: frozenset[OrderState] = frozenset(
    {
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.DENIED,
        OrderState.REJECTED,
        OrderState.FAILED,
    }
)


@dataclass(slots=True)
class Order:
    """One order's saga state, keyed by ``cloid`` and advanced by ``apply``."""

    cloid: str
    strategy_id: str
    signal_id: str
    symbol: str
    side: Side
    quantity: Decimal
    order_type: OrderType
    state: OrderState = OrderState.PENDING
    cum_qty: Decimal = Decimal("0")
    venue_oid: str | None = None
    reason: str | None = None
    cancel_requested: bool = False
    cancel_requested_ts: int | None = None
    cancel_signal_id: str | None = None
    _applied_event_ids: set[str] = field(default_factory=set, init=False, repr=False)

    @property
    def applied_event_ids(self) -> frozenset[str]:
        """The reflected ``event_id``s — the dedup set a ``Store`` round-trips."""
        return frozenset(self._applied_event_ids)

    @property
    def is_terminal(self) -> bool:
        """Whether the saga has reached a terminal state (no transition leaves it)."""
        return self.state in _TERMINAL_STATES

    def request_cancel(self, *, signal_id: str, ts_ns: int) -> bool:
        """Record cancel intent as a durable marker, **not** an FSM state (ADR-0026).

        The order stays in its current state and can still fill — the cancel/fill
        race is real, and a fill wins it. Returns ``True`` if this call set the
        marker, ``False`` if it was a no-op (already requested, or terminal), so
        the caller can skip a redundant cancel send. Idempotent: a re-emitted
        ``CancelSignal`` keeps the first request's ``signal_id`` and timestamp.

        The cancel's own ``signal_id`` is retained so a restart recovers the seq
        high-water-mark from the store and never re-issues a consumed cancel id
        (ADR-0026, ADR-0016) — the ``PlaceSignal`` id lives in ``signal_id``, but
        cancels have no other durable home.
        """
        if self.is_terminal or self.cancel_requested:
            return False
        self.cancel_requested = True
        self.cancel_requested_ts = ts_ns
        self.cancel_signal_id = signal_id
        return True

    @classmethod
    def restore(
        cls,
        *,
        cloid: str,
        strategy_id: str,
        signal_id: str,
        symbol: str,
        side: Side,
        quantity: Decimal,
        order_type: OrderType,
        state: OrderState,
        cum_qty: Decimal,
        venue_oid: str | None,
        reason: str | None,
        cancel_requested: bool,
        cancel_requested_ts: int | None,
        cancel_signal_id: str | None,
        applied_event_ids: Iterable[str],
    ) -> "Order":
        """Rebuild a checkpointed saga exactly as persisted (ADR-0008 recovery).

        Restores the dedup set too, so a redelivered event that predates the
        crash is still a no-op on the recovered saga. The ``cancel_requested``
        marker and the cancel's ``signal_id`` round-trip so reconciliation can
        resolve an ack-lost cancel and the seq high-water-mark stays intact after
        a restart (ADR-0026).
        """
        order = cls(
            cloid=cloid,
            strategy_id=strategy_id,
            signal_id=signal_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=order_type,
            state=state,
            cum_qty=cum_qty,
            venue_oid=venue_oid,
            reason=reason,
            cancel_requested=cancel_requested,
            cancel_requested_ts=cancel_requested_ts,
            cancel_signal_id=cancel_signal_id,
        )
        order._applied_event_ids.update(applied_event_ids)
        return order

    def apply(self, event: OrderEvent) -> bool:
        """Advance the saga by ``event``; idempotent and transition-checked.

        Returns ``True`` if the event advanced the saga, ``False`` if it was a
        deduped no-op — so a caller can suppress the downstream publish of a
        redelivered transition (at-least-once idempotency, ADR-0002).
        """
        if event.event_id in self._applied_event_ids:
            return False  # Already reflected — duplicate delivery is a no-op.

        target = event.state
        if (self.state, target) not in _LEGAL_TRANSITIONS:
            raise InvariantViolation(
                f"illegal saga transition {self.state.value} -> {target.value} "
                f"for cloid {self.cloid}"
            )

        self.state = target
        if event.venue_oid is not None:
            self.venue_oid = event.venue_oid
        if isinstance(event, OrderFillEvent):
            self.cum_qty = event.cum_qty
        if isinstance(event, OrderDenied | OrderRejected | OrderFailed):
            self.reason = event.reason

        self._applied_event_ids.add(event.event_id)
        return True

    def record_fill(
        self,
        *,
        trade_id: str,
        quantity: Decimal,
        price: Decimal,
        ts_event: int,
        ts_init: int,
    ) -> OrderFillEvent | None:
        """Account a raw fill and return the canonical event to publish, or ``None``.

        The single home for fill accounting: it accumulates ``cum_qty``, selects
        ``OrderFilled`` (fully filled) versus ``OrderPartiallyFilled`` (remainder
        working), builds the canonical event from this order's own identity, and
        delegates the transition to ``apply`` — the sole dedup and legality
        authority. Returns ``None`` on a redelivered ``trade_id`` (a deduped
        no-op) so the caller suppresses the duplicate publish; ``cum_qty`` can
        never double-count.
        """
        cum_qty = self.cum_qty + quantity
        event_type = OrderFilled if cum_qty >= self.quantity else OrderPartiallyFilled
        event = event_type(
            ts_event=ts_event,
            ts_init=ts_init,
            cloid=self.cloid,
            strategy_id=self.strategy_id,
            signal_id=self.signal_id,
            symbol=self.symbol,
            venue_oid=self.venue_oid,
            trade_id=trade_id,
            quantity=quantity,
            price=price,
            cum_qty=cum_qty,
        )
        return event if self.apply(event) else None
