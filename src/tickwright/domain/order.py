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

from dataclasses import dataclass, field
from decimal import Decimal

from .enums import OrderState, OrderType, Side
from .errors import InvariantViolation
from .events import OrderDenied, OrderEvent, OrderFailed, OrderFillEvent, OrderRejected

# Legal saga transitions as (from_state, to_state) pairs (ADR-0007).
_LEGAL_TRANSITIONS: frozenset[tuple[OrderState, OrderState]] = frozenset(
    {
        (OrderState.PENDING, OrderState.SUBMITTED),
        (OrderState.PENDING, OrderState.DENIED),
        (OrderState.SUBMITTED, OrderState.LIVE),
        (OrderState.SUBMITTED, OrderState.PARTIALLY_FILLED),
        (OrderState.SUBMITTED, OrderState.FILLED),
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
    _applied_event_ids: set[str] = field(default_factory=set, init=False, repr=False)

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
