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
    VenueOrderView,
)

from .cache import Cache


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

    async def _adopt(self, order: Order, view: VenueOrderView) -> None:
        """Align one saga with the venue's view of its cloid."""
        if not view.has_record:
            # A successful read with no status and no fills is positive proof
            # the order never landed: resolve FAILED (ADR-0010/0011) — never a
            # blind resend (ADR-0008 rule 2). Recreating is the strategy's call.
            await self._bus.publish(self._failed_verdict(order))
            return
        if view.status is not None:
            # Replay the venue's record as a synthetic, provenance-flagged fact;
            # the ExecutionManager turns it into the canonical transition.
            await self._bus.publish(replace(view.status, reconciliation=True))

    def _failed_verdict(self, order: Order) -> OrderStatusReport:
        now = self._clock.timestamp_ns()
        return OrderStatusReport(
            ts_event=now,
            ts_init=now,
            cloid=order.cloid,
            symbol=order.symbol,
            status=OrderState.FAILED,
            reason="reconciliation: venue has no record of this cloid",
            reconciliation=True,
        )
