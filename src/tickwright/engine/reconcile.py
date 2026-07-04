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

from dataclasses import replace

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

from .cache import Cache

_NS_PER_SECOND = 1_000_000_000


class Reconciler:
    """Compares local non-terminal sagas against venue truth and heals the gap."""

    def __init__(self, *, bus: EventBus, clock: Clock, exchange: Exchange, cache: Cache) -> None:
        self._bus = bus
        self._clock = clock
        self._exchange = exchange
        self._cache = cache

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
