"""``InMemoryBus`` — synchronous in-loop dispatch with a drain-to-quiescence FIFO.

The whole ADR-0023 dispatch model lives here. ``publish`` awaits each matching
subscriber inline, in subscription order (natural backpressure, no per-subscriber
queue). A ``publish`` called from inside a handler appends to one central FIFO and
returns immediately; the top-level call keeps draining until the FIFO is empty.

That FIFO trampoline buys three things depth-first recursion cannot: every
subscriber of an event runs before any event it spawned (order-independence, so a
MARKET order fills against the correctly-cached latest tick), long cascades
iterate rather than recurse (bounded stack), and the breadth-first order mirrors
the ``KafkaBus`` poll loop (backend parity).
"""

from collections import deque

from tickwright.domain import Event
from tickwright.domain.protocols import Handler


class InMemoryBus:
    """An ``EventBus`` that dispatches synchronously in the current event loop."""

    def __init__(self) -> None:
        self._subscriptions: list[tuple[type[Event], Handler[Event]]] = []
        self._queue: deque[Event] = deque()
        self._draining = False

    def subscribe[E: Event](self, event_type: type[E], handler: Handler[E]) -> None:
        # Handler[E] is stored as Handler[Event]; dispatch guards with isinstance,
        # so the handler only ever sees events it is registered for.
        self._subscriptions.append((event_type, handler))  # type: ignore[arg-type]

    async def publish(self, event: Event) -> None:
        self._queue.append(event)
        if self._draining:
            # Reentrant publish: enqueue and unwind — the active drain will reach it.
            return
        self._draining = True
        try:
            while self._queue:
                current = self._queue.popleft()
                for event_type, handler in list(self._subscriptions):
                    if isinstance(current, event_type):
                        await handler(current)
        finally:
            self._draining = False
