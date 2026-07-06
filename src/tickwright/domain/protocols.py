"""The seam Protocols — the swappable boundaries of the engine (ADR-0015).

These are structural contracts: an adapter satisfies a seam by shape, never by
inheritance, so ``domain`` stays a pure leaf that every impl compiles against
without importing anything back. Each Protocol is deliberately minimal for this
tracer slice; later slices widen them (``Exchange.cancel``/``fetch_*``,
``Clock`` timers, strategy snapshots) as their behaviors land.
"""

from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Protocol, runtime_checkable

from .events import Event, MarketTick, OrderEvent, PlaceOrder, PlaceSignal, VenueOrderView
from .instrument import GuardDecision, InstrumentSpec, KillSwitchState
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

    def snapshot(self) -> bytes:
        """This strategy's state *content* as opaque bytes (ADR-0016).

        The engine persists them per ``strategy_id`` — the strategy owns what
        they mean, never where they live. Keep state minimal and
        reconstructible; seq-safety never depends on it."""
        ...

    def restore(self, data: bytes) -> None:
        """Rebuild state content from bytes a prior ``snapshot()`` returned.

        Raising here is *not* fatal: the engine logs
        ``strategy.snapshot_incompatible`` and the strategy starts fresh
        (ADR-0016 — a strategy whose code changed shape between runs must not
        fault the engine)."""
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

    def save_strategy_snapshot(self, strategy_id: str, data: bytes, *, ts_ns: int) -> None:
        """Durably record ``strategy_id``'s opaque state bytes (ADR-0016).

        The engine persists what ``Strategy.snapshot()`` returned — content is
        the strategy's business, durability is ours. Latest snapshot wins; the
        same synchronous, no-yield discipline as ``checkpoint``."""
        ...

    def load_strategy_snapshot(self, strategy_id: str) -> bytes | None:
        """The last persisted snapshot for ``strategy_id``, or ``None`` if never
        saved — the read the engine restores from at startup (ADR-0016)."""
        ...

    def save_kill_switch(self, *, tripped: bool, reason: str | None, ts_ns: int) -> None:
        """Durably record the global kill-switch state (ADR-0026).

        Sticky by design: a tripped halt must outlive the process that set it,
        so the flag is persisted here and restored before the feed starts. The
        same synchronous, no-yield discipline as ``checkpoint``."""
        ...

    def load_kill_switch(self) -> "KillSwitchState | None":
        """The persisted kill-switch state, or ``None`` if never written — the
        read the guard restores from on startup (ADR-0026). ``None`` means never
        tripped; a restored ``tripped`` halt is cleared only by an explicit
        reset."""
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

    def instrument_specs(self) -> Mapping[str, InstrumentSpec]:
        """The per-symbol ``InstrumentSpec``s this venue owns (ADR-0031).

        The adapter is the one component that knows the venue (config on paper,
        the meta endpoint on Hyperliquid); it authors the specs and exposes them
        here so the ``Engine`` can wire them into the venue-agnostic guard at
        startup. A synchronous accessor, not a bus message — it is read once
        during composition, not a per-order hot path."""
        ...


@runtime_checkable
class PreTradeGuard(Protocol):
    """The thin pre-trade boundary the ``ExecutionManager`` runs before any send
    (ADR-0017): check → quantize → verdict. Not a RiskEngine — no positions,
    exposure, or portfolio risk (those are deferred).

    Two impls satisfy it: a ``RealGuard`` (min-notional, quantization, kill
    switch) and a ``NoopGuard`` passthrough. Users may plug their own — the
    Protocol-extensibility story.
    """

    def check(self, signal: PlaceSignal) -> GuardDecision:
        """Verdict on ``signal``: approve with quantized values, or ``DENIED``.

        Failure — a size that rounds to zero, a below-min-notional order, or a
        tripped kill switch — is ``DENIED`` (ADR-0010): never sent, safe to
        recreate. Approval carries the quantized ``quantity``/``price`` the order
        is actually placed at.

        The guard's specs are **mandatory**: a ``PlaceSignal`` for a symbol it has
        no ``InstrumentSpec`` for is a composition-root wiring bug (ADR-0031) and
        raises ``InvariantViolation`` (fail-fast, ADR-0014) — it cannot quantize
        without a spec, and must not send an unquantized order. This is the
        deliberate counterpart to ``Exchange``, whose specs are *optional* venue
        config (a missing one skips the venue-side min-notional check)."""
        ...

    def trip_kill_switch(self, reason: str) -> None:
        """Halt new placements globally and durably (ADR-0026). Every subsequent
        ``check`` returns ``DENIED``; resting ``LIVE`` orders are untouched. The
        halt is persisted, so it outlives a crash."""
        ...

    def reset_kill_switch(self) -> None:
        """Clear the halt and re-enable placement (ADR-0026). The only way a
        tripped kill switch is lifted — a crash never un-halts it."""
        ...

    @property
    def kill_switch_tripped(self) -> bool:
        """Whether the kill switch is currently tripped."""
        ...
