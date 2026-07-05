"""``Reconciler`` — the correctness net against the venue (ADR-0009/0011).

Two phases, one healing discipline. *Startup* (recovery step 3): after the
``Cache`` is rebuilt from the ``Store``, every non-terminal saga is reconciled
against venue truth by cloid **before anything can be placed**. *Continuous*:
two cycles thereafter — a fast in-flight check resolving ``SUBMITTED`` orders
that never acked, and a slower open-order/ghost reconcile in which only
continuous absence across the grace window (with a fill-history cross-check on
every read) resolves a resting order terminally. Each heal is a
``reconciliation``-flagged synthetic replica of a raw venue fact, published on
the bus and routed through the ``ExecutionManager`` — the one saga writer — so
dedup by ``event_id`` and ``trade_id`` makes every pass idempotent: re-running
converges.

A failed venue read (``fetch_order`` → ``None``) freezes the running pass: it
reports failure, emits ``reconcile.frozen``, and heals nothing it could not
prove — an outage must never read as "all orders vanished" (ADR-0011 inv 1).
"""

from collections.abc import Awaitable, Callable
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

from .absence import ConsecutiveMisses
from .cache import Cache
from .ghost_gate import GhostGate, GhostVerdict

_NS_PER_SECOND = 1_000_000_000

# The per-cadence state filters gating which sagas each continuous cycle reads.
# Startup filters nothing — it reconciles every non-terminal saga.
_INFLIGHT_STATES = frozenset({OrderState.SUBMITTED})
_OPEN_ORDER_STATES = frozenset({OrderState.LIVE, OrderState.PARTIALLY_FILLED})


@dataclass(frozen=True, slots=True, kw_only=True)
class ReconcileConfig:
    """Timing knobs for the continuous loops (ADR-0011 defaults).

    Construction enforces the timing invariant (ADR-0008/0011 rule 7): the
    in-flight retry budget — ``inflight_interval_seconds`` ×
    ``inflight_max_attempts`` — stays strictly under ``ghost_grace_seconds``,
    so no runtime path ever holds a config under which an order still being
    retried could be ghosted as missing.

    ``recent_order_protection_seconds`` is the second clause of ADR-0011 inv 3:
    the ghost cycle skips a resting order whose last saga event is fresher than
    this, so a just-acked order the venue's open-orders snapshot has not yet
    propagated is never raced onto the ghost path. Construction also holds it
    strictly under ``ghost_grace_seconds`` — the protection pre-filter is a brief
    shield that defers the *start* of the grace measurement, so it must not
    outlast the measurement it precedes.
    """

    inflight_interval_seconds: float = 5.0
    inflight_max_attempts: int = 3
    open_order_interval_seconds: float = 30.0
    ghost_grace_seconds: float = 90.0
    recent_order_protection_seconds: float = 30.0

    def __post_init__(self) -> None:
        for name in (
            "inflight_interval_seconds",
            "inflight_max_attempts",
            "open_order_interval_seconds",
            "ghost_grace_seconds",
            "recent_order_protection_seconds",
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
        if self.recent_order_protection_seconds >= self.ghost_grace_seconds:
            raise ValueError(
                f"the recent-order protection window "
                f"({self.recent_order_protection_seconds}s) must stay under the "
                f"ghost grace window ({self.ghost_grace_seconds}s): the protection "
                "pre-filter must not outlast the grace measurement it precedes "
                "(ADR-0011 inv 3)"
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
        # Continuous-cycle absence bookkeeping (ADR-0011 inv 3/7), both deliberately
        # in-memory — a restart resets them, and the startup pass re-proves
        # everything against venue truth anyway (ADR-0009). The in-flight retry
        # budget counts consecutive missed reads; the ghost gate owns inv 3 in full
        # — the recent-order protection pre-filter in front of the grace window —
        # so the "is it a ghost yet?" timing rule lives in one place.
        self._inflight_run = ConsecutiveMisses(limit=self._config.inflight_max_attempts)
        self._ghost_gate = GhostGate(
            grace_span_ns=int(self._config.ghost_grace_seconds * _NS_PER_SECOND),
            protection_span_ns=int(self._config.recent_order_protection_seconds * _NS_PER_SECOND),
        )

    async def reconcile_startup(self) -> bool:
        """One mass-rebuild pass over every non-terminal saga; ``True`` on success.

        ``False`` means a venue read failed and the pass froze — the caller
        (the startup barrier) retries; nothing was guessed in the meantime.
        """
        return await self._drive("startup", None, self._adopt)

    async def reconcile_inflight(self) -> bool:
        """The fast continuous cycle: resolve ``SUBMITTED`` orders that never
        acked — the riskiest "did it land?" path (ADR-0011). ``True`` on a
        completed pass; ``False`` means a venue read failed and the cycle froze.
        """
        return await self._drive("inflight", _INFLIGHT_STATES, self._resolve_inflight)

    async def reconcile_open_orders(self) -> bool:
        """The slow continuous cycle: resting orders and ghosts (ADR-0011).

        An order whose venue record is gone becomes a ghost candidate; only
        **continuous** absence across the grace window resolves it terminally,
        and every read doubles as the fill-history cross-check — a vanished
        order may have filled. ``True`` on a completed pass; ``False`` means a
        venue read failed and the cycle froze.
        """
        return await self._drive("open_order", _OPEN_ORDER_STATES, self._resolve_open_order)

    async def _drive(
        self,
        cycle: str,
        states: frozenset[OrderState] | None,
        handle: Callable[[Order, VenueOrderView], Awaitable[None]],
    ) -> bool:
        """The one skeleton every cadence shares: over each non-terminal saga in
        ``states`` (all of them when ``None``), read venue truth by cloid and let
        ``handle`` resolve the gap. A failed read (``None``) trips the connectivity
        guard — the whole pass freezes here and nowhere else, so ADR-0011 inv 1
        lives in exactly one place. ``True`` on a completed pass, ``False`` when a
        read froze it.
        """
        for order in self._cache.open_orders():
            if states is not None and order.state not in states:
                continue
            view = await self._exchange.fetch_order(order.cloid)
            if view is None:
                return self._freeze(cycle, order.cloid)
            await handle(order, view)
        return True

    async def _resolve_inflight(self, order: Order, view: VenueOrderView) -> None:
        """Resolve one ``SUBMITTED`` order against its venue reading. A record
        proves the send landed → adopt it. No record is not yet proof — the send
        may still be on the wire in this very life — so only the budget-exhausting
        consecutive miss resolves ``FAILED``, never a blind resend. That budget
        stays under the ghost grace window, so a retried order can never be
        ghosted (ADR-0008/0011)."""
        if view.has_record:
            self._inflight_run.record_present(order.cloid)
            await self._adopt(order, view)
            return
        if self._inflight_run.record_absent(order.cloid):
            await self._bus.publish(self._failed_verdict(order))
            named_event(
                "inflight.reconciled", cloid=order.cloid, resolution=OrderState.FAILED.value
            )

    async def _resolve_open_order(self, order: Order, view: VenueOrderView) -> None:
        """Resolve one resting order against its venue reading. A live record
        resets the grace clock and adopts. Otherwise the read doubled as the
        fill-history cross-check (ADR-0011 inv 2/4): a vanished order may have
        filled, and executed truth heals immediately — no grace wait. A
        just-acked order still too recent to have propagated is left untouched
        (no grace-arming, no ghost) rather than raced. Only an order still
        non-terminal, past its protection window, *and* continuously absent
        across the grace window is ghost-resolved."""
        if view.status is not None:
            self._ghost_gate.record_present(order.cloid)
            await self._adopt(order, view)
            return
        await self._heal_fills(view)
        if order.is_terminal:
            self._ghost_gate.record_present(order.cloid)
            return
        verdict = self._ghost_gate.evaluate(
            order.cloid,
            now_ns=self._clock.timestamp_ns(),
            last_event_ns=self._cache.last_event_ts(order.cloid),
        )
        if verdict is GhostVerdict.PROTECTED:
            named_event("reconcile.recency_skipped", cloid=order.cloid)
        elif verdict is GhostVerdict.GHOST:
            await self._resolve_ghost(order)

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
        await self._heal_fills(view)
        if view.status is not None and not (view.status.status is OrderState.LIVE and view.fills):
            # A LIVE record alongside fills is stale by definition — the venue
            # reported it working before it executed; the fills are the truth.
            await self._bus.publish(replace(view.status, reconciliation=True))

    async def _heal_fills(self, view: VenueOrderView) -> None:
        """Replay the view's fill history as reconciliation-flagged replicas —
        the venue-pushed twin of each carries the same ``event_id``, so a late
        duplicate collapses under the ``ExecutionManager``'s dedup."""
        for fill in view.fills:
            await self._bus.publish(replace(fill, reconciliation=True))

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
