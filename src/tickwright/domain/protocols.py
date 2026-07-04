"""The seam Protocols — the swappable boundaries of the engine (ADR-0015).

These are structural contracts: an adapter satisfies a seam by shape, never by
inheritance, so ``domain`` stays a pure leaf that every impl compiles against
without importing anything back. Each Protocol is deliberately minimal for this
tracer slice; later slices widen them (``Exchange.cancel``/``fetch_*``,
``Clock`` timers, strategy snapshots) as their behaviors land.
"""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Protocol, runtime_checkable

from .events import Event, MarketTick, OrderEvent, PlaceOrder, VenueOrderView
from .order import Order

type Handler[E: Event] = Callable[[E], Awaitable[None]]
"""An async subscriber of a single event family."""


@runtime_checkable
class EventBus(Protocol):
    """Publish/subscribe transport (ADR-0023). Pub/sub only — no query surface."""

    def subscribe[E: Event](self, event_type: type[E], handler: Handler[E]) -> None:
        """Register ``handler`` for every published event that is an ``event_type``."""
        ...

    async def publish(self, event: Event) -> None:
        """Publish ``event``, draining the whole reentrant cascade to quiescence."""
        ...


@runtime_checkable
class Clock(Protocol):
    """The injected source of all time (ADR-0005). Canonical unit: UTC epoch ns."""

    def timestamp_ns(self) -> int:
        """The current time as UTC epoch nanoseconds."""
        ...

    def now(self) -> datetime:
        """The current time as a timezone-aware UTC ``datetime`` (human edges only)."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Wait ``seconds``; virtual and immediate under ``ManualClock``."""
        ...


@runtime_checkable
class ReplayClock(Clock, Protocol):
    """A ``Clock`` whose virtual time a deterministic producer drives forward.

    ``ReplayFeed`` advances the clock to each row's ``ts_event`` before publishing
    (ADR-0027). Declaring the capability here — not on the concrete ``ManualClock``
    — lets the feed depend on ``domain`` alone, keeping the no-adapter-imports-an-
    adapter boundary intact (ADR-0032). ``LiveClock`` implements only ``Clock``.
    """

    def advance_to(self, ts_ns: int) -> None:
        """Advance virtual time to ``ts_ns`` (never backward)."""
        ...


@runtime_checkable
class MarketFeed(Protocol):
    """Produces ``MarketTick`` events for configured symbols (ADR-0015)."""

    async def start(self) -> None:
        """Begin producing ticks. ``ReplayFeed`` runs to end-of-file."""
        ...

    async def stop(self) -> None:
        """Stop producing ticks."""
        ...


@runtime_checkable
class Strategy(Protocol):
    """Consumes ticks, reacts to lifecycle, emits ``Signal``s (ADR-0015/0016)."""

    strategy_id: str

    async def on_tick(self, tick: MarketTick) -> None:
        """Handle a market tick; may emit signals."""
        ...

    async def on_order_event(self, event: OrderEvent) -> None:
        """Handle a canonical saga transition for one of this strategy's orders."""
        ...


@runtime_checkable
class Store(Protocol):
    """Durable saga checkpoints (ADR-0019). The write the crash-safety
    argument rests on (ADR-0008).

    Deliberately synchronous: a checkpoint is one atomic step of a handler —
    ``apply`` then persist with no yield point in between — so no other
    handler can ever observe a saga whose memory and durable states disagree.
    Throughput is explicitly not a goal; readable recovery is.
    """

    def checkpoint(self, order: Order, *, ts_ns: int) -> None:
        """Durably record ``order``'s full saga state as of ``ts_ns``."""
        ...

    def get_order(self, cloid: str) -> Order | None:
        """Rebuild the checkpointed saga for ``cloid``, or ``None`` if unknown."""
        ...

    def all_orders(self) -> list[Order]:
        """Rebuild every checkpointed saga — the recovery mass-read the ``Cache``
        projection is rebuilt from (ADR-0009)."""
        ...


@runtime_checkable
class Exchange(Protocol):
    """A thin venue boundary adapter (ADR-0015): translate and emit raw facts.

    Owns no saga. ``place`` emits ``ExecutionReport``s on the bus rather than
    returning them, so the ``ExecutionManager`` drives the FSM off venue facts.
    """

    async def place(self, order: PlaceOrder) -> None:
        """Place ``order`` at the venue; emit the resulting raw ``ExecutionReport``(s)."""
        ...

    async def cancel(self, cloid: str) -> None:
        """Cancel the order identified by ``cloid``; emit the resulting raw
        ``ExecutionReport``. A cancel of an unknown/already-gone order is a
        benign no-op (ADR-0026)."""
        ...

    async def fetch_order(self, cloid: str) -> VenueOrderView | None:
        """Venue truth for ``cloid`` — the reconciler's query-shaped direct read
        (ADR-0004), never a bus message. Returns ``None`` only when the read
        itself failed (outage): a failed read must never look like "no record"
        (ADR-0011 inv 1). A successful read always returns a view, even an
        empty one."""
        ...
