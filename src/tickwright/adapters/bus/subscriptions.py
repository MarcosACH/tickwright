"""``Subscriptions`` — the fan-out both ``EventBus`` backends share (ADR-0023).

The registry of ``(event_type, handler)`` pairs plus the per-event dispatch that
delivers an event to every subscriber it ``isinstance``-matches, in registration
order. This is the one piece of the bus that parity *requires* be identical
across backends — a handler seeing only its own event family, always in the same
order — so it lives here once rather than as two copies the two adapters must
keep in lockstep. Each backend keeps its own concern around it: the in-memory
FIFO trampoline, the Kafka poll loop and offset commits. ``dispatch`` lets a
handler exception propagate; the backend that called it owns containment.
"""

from tickwright.domain import Event
from tickwright.domain.protocols import Handler


class Subscriptions:
    """Type-guarded, registration-ordered delivery of one event to its subscribers."""

    def __init__(self) -> None:
        self._entries: list[tuple[type[Event], Handler[Event]]] = []

    def subscribe[E: Event](self, event_type: type[E], handler: Handler[E]) -> None:
        # Handler[E] is stored as Handler[Event]; dispatch guards with isinstance,
        # so the handler only ever sees events it is registered for.
        self._entries.append((event_type, handler))  # type: ignore[arg-type]

    async def dispatch(self, event: Event) -> None:
        """Await every subscriber whose type matches ``event``, in registration order.

        Iterates a snapshot so a handler subscribing mid-dispatch neither
        disturbs the live iteration nor is delivered to until the next event.
        """
        for event_type, handler in list(self._entries):
            if isinstance(event, event_type):
                await handler(event)
