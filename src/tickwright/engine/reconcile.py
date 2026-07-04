"""``Reconciler`` — startup mass-rebuild against the venue (ADR-0009/0011).

Recovery step 3: after the ``Cache`` is rebuilt from the ``Store``, every
non-terminal saga is reconciled against venue truth by cloid **before anything
can be placed**. Each heal is a ``reconciliation``-flagged synthetic replica of
a raw venue fact, published on the bus and routed through the
``ExecutionManager`` — the one saga writer — so dedup by ``event_id`` and
``trade_id`` makes recovery idempotent: re-running a pass converges.

A failed venue read (``fetch_order`` → ``None``) freezes the pass: it reports
failure and heals nothing it could not prove — an outage must never read as
"all orders vanished" (ADR-0011 inv 1). The continuous loops are a later slice
(#15); this module owns the startup phase.
"""

from dataclasses import dataclass, replace

from tickwright.domain import (
    Clock,
    EventBus,
    Exchange,
    Order,
    OrderState,
    OrderStatusReport,
    StartupReconciliationTimeout,
    VenueOrderView,
)
from tickwright.observability import named_event

from .cache import Cache

_NS_PER_SECOND = 1_000_000_000


@dataclass(frozen=True, slots=True, kw_only=True)
class ReconcileConfig:
    """Timing knobs for the continuous loops (ADR-0011 defaults).

    Construction enforces the timing invariant (ADR-0008/0011 rule 7): the
    in-flight retry budget — ``inflight_interval_seconds`` ×
    ``inflight_max_attempts`` — stays strictly under ``ghost_grace_seconds``,
    so no runtime path ever holds a config under which an order still being
    retried could be ghosted as missing.
    """

    inflight_interval_seconds: float = 5.0
    inflight_max_attempts: int = 3
    open_order_interval_seconds: float = 30.0
    ghost_grace_seconds: float = 90.0

    def __post_init__(self) -> None:
        for name in (
            "inflight_interval_seconds",
            "inflight_max_attempts",
            "open_order_interval_seconds",
            "ghost_grace_seconds",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        budget = self.inflight_interval_seconds * self.inflight_max_attempts
        if budget >= self.ghost_grace_seconds:
            raise ValueError(
                f"in-flight retry budget ({budget}s) must stay under the "
                f"ghost grace window ({self.ghost_grace_seconds}s): an order "
                "still being retried must never be ghosted (ADR-0008/0011)"
            )


class Reconciler:
    """Compares local non-terminal sagas against venue truth and heals the gap."""

    def __init__(
        self,
        *,
        bus: EventBus,
        clock: Clock,
        exchange: Exchange,
        cache: Cache,
        config: ReconcileConfig | None = None,
    ) -> None:
        self._bus = bus
        self._clock = clock
        self._exchange = exchange
        self._cache = cache
        self._config = config if config is not None else ReconcileConfig()
        # In-memory continuous-cycle bookkeeping: consecutive no-record reads
        # per in-flight cloid, and when each resting order was first observed
        # missing its venue record. Deliberately not durable — a restart resets
        # both, and the startup pass re-proves everything anyway (ADR-0009).
        self._inflight_misses: dict[str, int] = {}
        self._absent_since_ns: dict[str, int] = {}

    async def reconcile_startup(self) -> bool:
        """One mass-rebuild pass over every non-terminal saga; ``True`` on success.

        ``False`` means a venue read failed and the pass froze — the caller
        (the startup barrier) retries; nothing was guessed in the meantime.
        """
        for order in self._cache.open_orders():
            view = await self._exchange.fetch_order(order.cloid)
            if view is None:
                return False
            await self._adopt(order, view)
        return True

    async def reconcile_inflight(self) -> bool:
        """The fast continuous cycle: resolve ``SUBMITTED`` orders that never
        acked — the riskiest "did it land?" path (ADR-0011). ``True`` on a
        completed pass; ``False`` means a venue read failed and the cycle froze.
        """
        for order in self._cache.open_orders():
            if order.state is not OrderState.SUBMITTED:
                continue
            view = await self._exchange.fetch_order(order.cloid)
            if view is None:
                return self._freeze("inflight", order.cloid)
            if view.has_record:
                self._inflight_misses.pop(order.cloid, None)
                await self._adopt(order, view)
                continue
            # No record is not yet proof: the send may still be on the wire in
            # this very life. Only the budget-exhausting consecutive miss
            # resolves FAILED — and that budget stays under the ghost grace
            # window, so a retried order can never be ghosted (ADR-0008/0011).
            misses = self._inflight_misses.get(order.cloid, 0) + 1
            if misses < self._config.inflight_max_attempts:
                self._inflight_misses[order.cloid] = misses
                continue
            self._inflight_misses.pop(order.cloid, None)
            await self._bus.publish(self._failed_verdict(order))
        return True

    async def reconcile_open_orders(self) -> bool:
        """The slow continuous cycle: resting orders and ghosts (ADR-0011).

        An order whose venue record is gone becomes a ghost candidate; only
        **continuous** absence across the grace window resolves it terminally,
        and every read doubles as the fill-history cross-check — a vanished
        order may have filled. ``True`` on a completed pass; ``False`` means a
        venue read failed and the cycle froze.
        """
        grace_ns = int(self._config.ghost_grace_seconds * _NS_PER_SECOND)
        for order in self._cache.open_orders():
            if order.state not in (OrderState.LIVE, OrderState.PARTIALLY_FILLED):
                continue
            view = await self._exchange.fetch_order(order.cloid)
            if view is None:
                return self._freeze("open_order", order.cloid)
            if view.status is not None:
                # The record is back (or never left): not a ghost — the grace
                # clock resets so only *continuous* absence can resolve.
                self._absent_since_ns.pop(order.cloid, None)
                await self._adopt(order, view)
                continue
            # No open-order record, but the read doubled as the fill-history
            # cross-check (ADR-0011 inv 2/4): a vanished order may have filled,
            # and executed truth heals immediately — no grace wait.
            for fill in view.fills:
                await self._bus.publish(replace(fill, reconciliation=True))
            if order.is_terminal:
                self._absent_since_ns.pop(order.cloid, None)
                continue
            now = self._clock.timestamp_ns()
            first_absent_ns = self._absent_since_ns.setdefault(order.cloid, now)
            if now - first_absent_ns >= grace_ns:
                self._absent_since_ns.pop(order.cloid)
                await self._resolve_ghost(order)
        return True

    async def _resolve_ghost(self, order: Order) -> None:
        """Terminal resolution for an order continuously absent past the grace
        window (ADR-0010/0011/0026): our own durable ``cancel_requested`` marker
        reads the vanish as the cancel landing ack-lost → ``CANCELLED``;
        otherwise truly gone → ``REJECTED`` from ``LIVE``."""
        if order.cancel_requested:
            status, reason = OrderState.CANCELLED, "ghost: vanished after a requested cancel"
        elif order.state is OrderState.PARTIALLY_FILLED:
            # A rejection would deny fills that provably happened: CANCELLED
            # terminates the remainder while the executed quantity stands.
            status, reason = OrderState.CANCELLED, "ghost: vanished with fills preserved"
        else:
            status, reason = OrderState.REJECTED, "ghost: vanished from the venue"
        await self._bus.publish(self._verdict(order, status, reason))
        named_event("ghost.reconciled", cloid=order.cloid, resolution=status.value)

    def _freeze(self, cycle: str, cloid: str) -> bool:
        """The connectivity guard tripping (ADR-0011 inv 1): a failed venue read
        aborts the whole cycle — nothing is ghosted, removed, or counted, since
        an outage must never read as "all orders vanished". Returns ``False``
        for the caller to propagate as the cycle verdict."""
        named_event("reconcile.frozen", cycle=cycle, cloid=cloid)
        return False

    async def run_startup_barrier(
        self,
        *,
        timeout_seconds: float,
        initial_backoff_seconds: float = 1.0,
        max_backoff_seconds: float = 30.0,
    ) -> None:
        """The hard startup gate (ADR-0024): nothing places until this clears.

        Retries the mass-rebuild with exponential backoff so a transient
        boot-time venue blip resolves and startup proceeds; a sustained outage
        trips ``startup_reconciliation_timeout`` → ``StartupReconciliationTimeout``
        (an ``InvariantViolation``), which the runner maps to ``FAULTED`` and a
        non-zero exit for the external supervisor to backoff-restart. On the
        paper path reads cannot fail, so the barrier always clears.

        The backoff is capped at ``max_backoff_seconds`` so an uncapped doubling
        cannot carry the clock far past the deadline: without the cap a large
        ``timeout_seconds`` would fault nearly a whole backoff interval late,
        making real time-to-``FAULTED`` up to ~2× the configured window.
        """
        deadline_ns = self._clock.timestamp_ns() + int(timeout_seconds * _NS_PER_SECOND)
        backoff_seconds = initial_backoff_seconds
        while not await self.reconcile_startup():
            if self._clock.timestamp_ns() >= deadline_ns:
                raise StartupReconciliationTimeout(
                    f"venue unreachable for {timeout_seconds}s during startup "
                    "reconciliation; refusing to start on unverified state"
                )
            await self._clock.sleep(backoff_seconds)
            backoff_seconds = min(backoff_seconds * 2, max_backoff_seconds)

    async def _adopt(self, order: Order, view: VenueOrderView) -> None:
        """Align one saga with the venue's view of its cloid."""
        if not view.has_record:
            # A successful read with no status and no fills is positive proof
            # the order never landed: resolve FAILED (ADR-0010/0011) — never a
            # blind resend (ADR-0008 rule 2). Recreating is the strategy's call.
            await self._bus.publish(self._failed_verdict(order))
            return

        if order.state is OrderState.PENDING:
            # The venue has a record, so the send provably left the box: walk
            # the write-ahead intent through SUBMITTED first — the venue's
            # facts (LIVE, fills, ...) are only legal transitions from there.
            await self._bus.publish(self._submitted_bridge(order))

        # Replay the venue's facts as synthetic, provenance-flagged replicas;
        # the ExecutionManager turns them into canonical transitions, deduping
        # by trade_id/event_id anything the saga already reflects. Fills go
        # first: a terminal status (CANCELLED after a partial fill) is only
        # legal once the fills it followed are applied.
        for fill in view.fills:
            await self._bus.publish(replace(fill, reconciliation=True))
        if view.status is not None and not (view.status.status is OrderState.LIVE and view.fills):
            # A LIVE record alongside fills is stale by definition — the venue
            # reported it working before it executed; the fills are the truth.
            await self._bus.publish(replace(view.status, reconciliation=True))

    def _failed_verdict(self, order: Order) -> OrderStatusReport:
        return self._verdict(order, OrderState.FAILED, "venue has no record of this cloid")

    def _submitted_bridge(self, order: Order) -> OrderStatusReport:
        return self._verdict(order, OrderState.SUBMITTED, "venue record proves the send landed")

    def _verdict(self, order: Order, status: OrderState, reason: str) -> OrderStatusReport:
        """A reconciler verdict dressed as the synthetic status report that
        carries it through the ``ExecutionManager`` — always provenance-flagged."""
        now = self._clock.timestamp_ns()
        return OrderStatusReport(
            ts_event=now,
            ts_init=now,
            cloid=order.cloid,
            symbol=order.symbol,
            status=status,
            reason=f"reconciliation: {reason}",
            reconciliation=True,
        )
