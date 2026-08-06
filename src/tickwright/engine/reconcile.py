"""``Reconciler`` — the correctness net against the venue (ADR-0009/0011).

Two phases, one healing discipline. *Startup* (recovery step 3): after the
``Cache`` is rebuilt from the ``Store``, every non-terminal saga is reconciled
against venue truth by cloid **before anything can be placed** — as one step of
the ``StartupBarrier``, which owns the retry-then-fault policy and the ordering
against the account materialisation that precedes it (``barrier.py``).
*Continuous*: two cycles thereafter — a fast in-flight check resolving
``SUBMITTED`` orders that never acked, and a slower open-order/ghost reconcile
in which only continuous absence across the grace window (with a fill-history
cross-check on every read) resolves a resting order terminally. Each heal is a
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
from enum import Enum

from tickwright.domain import (
    Clock,
    EventBus,
    Exchange,
    Order,
    OrderState,
    OrderStatusReport,
    VenueOrderView,
    VenueReadFailure,
    VenueReadUnresolvable,
)
from tickwright.observability import NamedEvent, named_event
from tickwright.observability.correlation import operation

from .absence import ConsecutiveMisses
from .cache import Cache
from .ghost_gate import GhostGate, GhostVerdict

_NS_PER_SECOND = 1_000_000_000

# The per-cadence state filters gating which sagas each continuous cycle reads.
# Startup filters nothing — it reconciles every non-terminal saga.
_INFLIGHT_STATES = frozenset({OrderState.SUBMITTED})
_OPEN_ORDER_STATES = frozenset({OrderState.LIVE, OrderState.PARTIALLY_FILLED})


class _FreezeScope(Enum):
    """How much of a pass one failed read froze (ADR-0049) — the ``scope`` field
    on ``reconcile.frozen``.

    ``CYCLE`` is the whole remaining worklist: the venue did not answer, so
    nothing behind this order was read. ``ORDER`` is this order alone: the venue
    answered, unreadably, and every order behind it reconciled normally.
    """

    CYCLE = "cycle"
    ORDER = "order"


@dataclass(frozen=True, slots=True, kw_only=True)
class ReconcileConfig:
    """Timing knobs for the continuous loops (ADR-0011 defaults).

    Construction enforces the timing invariant (ADR-0008/0011 rule 7): the
    in-flight retry budget — ``inflight_interval_seconds`` ×
    ``inflight_max_attempts`` — stays strictly under ``ghost_grace_seconds``,
    so no runtime path ever holds a config under which an order still being
    retried could be ghosted as missing.

    ``unreadable_max_attempts`` is the per-cloid budget of ADR-0049: how many
    *consecutive* reads of one order may come back with a body the adapter
    cannot read before the condition is taken as durable and escalated. It is
    deliberately outside the timing invariant below — a skipped order is never
    counted absent, so this budget cannot race the ghost grace window the way
    the in-flight retry budget could.

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
    unreadable_max_attempts: int = 3

    def __post_init__(self) -> None:
        for name in (
            "inflight_interval_seconds",
            "inflight_max_attempts",
            "open_order_interval_seconds",
            "ghost_grace_seconds",
            "recent_order_protection_seconds",
            "unreadable_max_attempts",
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
        config: ReconcileConfig,
    ) -> None:
        self._bus = bus
        self._clock = clock
        self._exchange = exchange
        self._cache = cache
        # Required, not defaulted: the Engine resolves the one ReconcileConfig
        # and paces the cadence loops off the same intervals — a second default
        # here could drift the pacing apart from the judging.
        self._config = config
        # Continuous-cycle absence bookkeeping (ADR-0011 inv 3/7), both deliberately
        # in-memory — a restart resets them, and the startup pass re-proves
        # everything against venue truth anyway (ADR-0009). The in-flight retry
        # budget counts consecutive missed reads; the ghost gate owns inv 3 in full
        # — the recent-order protection pre-filter in front of the grace window —
        # so the "is it a ghost yet?" timing rule lives in one place.
        self._inflight_run = ConsecutiveMisses(limit=self._config.inflight_max_attempts)
        # The third run of the same shape, and the one that judges the *read*
        # rather than what it found (ADR-0049): consecutive unreadable bodies
        # for one cloid, cleared by any read that came back readable. A failed
        # send neither advances nor clears it — an outage is no evidence either
        # way about a body nobody received.
        self._unreadable_run = ConsecutiveMisses(limit=self._config.unreadable_max_attempts)
        self._ghost_gate = GhostGate(
            grace_span_ns=int(self._config.ghost_grace_seconds * _NS_PER_SECOND),
            protection_span_ns=int(self._config.recent_order_protection_seconds * _NS_PER_SECOND),
        )

    async def reconcile_startup(self) -> bool:
        """One mass-rebuild pass over every non-terminal saga; ``True`` on success.

        ``False`` means a venue read failed and the pass froze — the caller
        (the ``StartupBarrier``, which holds the retry-then-fault policy this
        step shares with the account materialisation ordered ahead of it)
        retries; nothing was guessed in the meantime.
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
        ``handle`` resolve the gap. A failed read trips the connectivity guard —
        the whole pass freezes here and nowhere else, so ADR-0011 inv 1 lives in
        exactly one place. ``True`` only on a pass that proved **every** order it
        looked at; ``False`` when any read failed.

        **How much a failed read costs turns on whether the venue answered**
        (ADR-0049). A dead send says nothing about this order in particular — the
        venue is unreachable, and every order behind it would pay a full request
        timeout to learn the same thing — so the pass stops at it. An unreadable
        body is the opposite: the venue is up and answering at full speed, so it
        says nothing about the orders behind it, and stopping on it lets one
        durably unreadable body stall every other order forever. That one order
        is skipped, its own budget is charged, and the pass carries on.

        The verdict is ``False`` either way, and that is what keeps the startup
        phase's contract intact (inv 5): a pass that could not prove one order
        never clears the ``StartupBarrier``, whichever way the read failed.
        """
        # The cycle id scopes the whole pass; the per-order cloid nests inside it
        # (ADR-0020), so every reconcile record — a verdict or a freeze — is
        # traceable to its cycle and its order without either being repeated as a
        # field. Correlation binding lives here, at the one cadence chokepoint.
        with operation(cycle=cycle):
            proved_all = True
            for order in self._cache.open_orders():
                if states is not None and order.state not in states:
                    continue
                with operation(cloid=order.cloid):
                    view = await self._exchange.fetch_order(order.cloid)
                    match view:
                        case VenueReadFailure.SEND_FAILED:
                            return self._freeze(_FreezeScope.CYCLE)
                        case VenueReadFailure.UNREADABLE_BODY:
                            self._charge_unreadable(order)
                            proved_all = False
                        case _:
                            self._unreadable_run.record_present(order.cloid)
                            await handle(order, view)
            return proved_all

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
            named_event(NamedEvent.INFLIGHT_RECONCILED, resolution=OrderState.FAILED.value)

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
            named_event(NamedEvent.RECONCILE_RECENCY_SKIPPED)
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
        named_event(NamedEvent.GHOST_RECONCILED, resolution=status.value)

    def _charge_unreadable(self, order: Order) -> None:
        """Charge one unreadable read against ``order``'s budget and freeze it —
        or escalate, once the budget says the condition is durable (ADR-0049).

        A single unreadable body is transient by assumption and stays one: the
        order is skipped, the pass carries on, and the next cycle re-reads it.
        What no single sample can show is whether waiting will ever help
        (ADR-0048 §1) — a truncated response resolves itself, a venue contract
        change never does — and consecutive reads across the cadence are the
        several samples that answer it.

        At the budget the answer is in, and the verdict is a fault rather than a
        resolution: nothing here read the order's state, so there is no terminal
        to resolve it to that would not be invented. ``read`` has already named
        each of these reads against this cloid with the body it could not read,
        so the refusal names the order and leaves the evidence where it is.
        """
        if not self._unreadable_run.record_absent(order.cloid):
            self._freeze(_FreezeScope.ORDER)
            return
        raise VenueReadUnresolvable(
            f"venue body for cloid {order.cloid} unreadable on "
            f"{self._config.unreadable_max_attempts} consecutive reads: the condition "
            "is durable and no later pass can resolve it — see the "
            "exchange.request_failed reads for this cloid, each quoting the body"
        )

    def _freeze(self, scope: "_FreezeScope") -> bool:
        """The connectivity guard tripping (ADR-0011 inv 1): a failed venue read
        heals nothing — nothing is ghosted, removed, or counted, since an outage
        must never read as "all orders vanished". Returns ``False`` for the
        caller to propagate as the cycle verdict. The frozen cycle and the order
        whose read failed ride the ambient context (ADR-0020).

        ``scope`` is what says **how much** froze, and it is a field rather than
        a second event name because the catalog is closed (ADR-0045) and nothing
        routes on the difference — but plenty reads it. Both scopes name the
        cloid whose read failed, so without this an operator watching
        ``reconcile.frozen`` cannot tell a pass that stopped dead from a pass
        that reconciled everything except one order (ADR-0049)."""
        named_event(NamedEvent.RECONCILE_FROZEN, scope=scope.value)
        return False

    async def _adopt(self, order: Order, view: VenueOrderView) -> None:
        """Align one saga with the venue's view of its cloid."""
        if not view.has_record:
            if order.state in _OPEN_ORDER_STATES:
                # The venue once ACKed this order as working, so an empty read
                # means it is *gone*, not un-sent: the ghost taxonomy applies —
                # REJECTED from LIVE, CANCELLED with fills preserved or after a
                # requested cancel — never FAILED (ADR-0010/0011 resolutions).
                await self._resolve_ghost(order)
                return
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
