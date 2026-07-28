"""``ExecutionManager`` — the single engine-internal saga orchestrator (ADR-0015).

Deliberately *not* a Protocol: there is one of it, and it owns the crash-safe saga
once so Paper and Hyperliquid are served identically. It subscribes to ``Signal``s
and raw ``ExecutionReport``s, assigns the ``cloid`` from the ``signal_id``, drives
``Order.apply``, and publishes the canonical ``OrderEvent``s strategies consume.

Every transition checkpoints durably (ADR-0008): the ``PENDING`` write-ahead
intent is persisted **before** any ``Exchange.place`` call — so a crash mid-send
leaves a durable record recovery can reconcile by cloid — and ``SUBMITTED`` is
checkpointed after the send returns, arming the in-flight grace clock. A timeout
never transitions a saga: nothing here subscribes to time; only venue facts and
reconciliation verdicts move an order — and a status arriving after the saga
already resolved terminally is absorbed as an idempotent no-op (ADR-0026).

A **fill** additionally moves the ``PortfolioProjection`` before it is published:
the manager is the projection's single writer, so a fill lands in the ledger
exactly once and a strategy reading its position from the ``OrderFilled`` handler
reads the state that fill produced (ADR-0035). No other transition touches it.

A ``CancelSignal`` re-derives the target ``cloid`` from ``target_signal_id``,
durably checkpoints the ``cancel_requested`` marker **before** calling
``Exchange.cancel``, and leaves the saga ``LIVE`` — the marker is metadata, not a
state, so the order can still fill (ADR-0026).

The ``PreTradeGuard`` runs **before** the write-ahead (ADR-0017): a ``Denied``
verdict mints the ``DENIED`` terminal and never touches the exchange, while an
``Approved`` verdict carries the quantized ``quantity``/``price`` the order is
actually placed at. It defaults to a passthrough ``NoopGuard`` so the paper/test
path needs no policy wired in.
"""

from decimal import Decimal

from tickwright.domain import (
    CancelSignal,
    Clock,
    Denied,
    EventBus,
    Exchange,
    ExecutionReport,
    FillReport,
    InvariantViolation,
    Order,
    OrderCancelled,
    OrderDenied,
    OrderEvent,
    OrderFailed,
    OrderFilled,
    OrderLive,
    OrderPartiallyFilled,
    OrderPlaced,
    OrderRejected,
    OrderState,
    OrderStatusReport,
    OrderSubmitted,
    PlaceOrder,
    PlaceSignal,
    PreTradeGuard,
    Signal,
    derive_cloid,
)
from tickwright.observability import NamedEvent, named_event
from tickwright.observability.correlation import operation

from .cache import Cache
from .guard import NoopGuard
from .portfolio import PortfolioProjection

# The saga transition → named event map (ADR-0020): every canonical ``OrderEvent``
# this manager publishes has exactly one cataloged name, so "a state-affecting
# path with no named event is a defect" is enforced by construction — a new
# ``OrderEvent`` family that is not mapped here fails ``_announce`` with a KeyError.
_SAGA_EVENTS: dict[type[OrderEvent], NamedEvent] = {
    OrderPlaced: NamedEvent.ORDER_PLACED,
    OrderSubmitted: NamedEvent.ORDER_SUBMITTED,
    OrderLive: NamedEvent.ORDER_LIVE,
    OrderPartiallyFilled: NamedEvent.ORDER_PARTIALLY_FILLED,
    OrderFilled: NamedEvent.ORDER_FILLED,
    OrderDenied: NamedEvent.ORDER_DENIED,
    OrderRejected: NamedEvent.ORDER_REJECTED,
    OrderFailed: NamedEvent.ORDER_FAILED,
    OrderCancelled: NamedEvent.ORDER_CANCELLED,
}


class ExecutionManager:
    """Owns cloid assignment, the saga FSM, and canonical ``OrderEvent`` publishing."""

    def __init__(
        self,
        *,
        bus: EventBus,
        clock: Clock,
        exchange: Exchange,
        cache: Cache,
        portfolio: PortfolioProjection,
        guard: PreTradeGuard | None = None,
    ) -> None:
        self._bus = bus
        self._clock = clock
        self._exchange = exchange
        # The working set and the durable copy in one seam: the Cache projects
        # every checkpoint and is rebuilt from the Store on restart (ADR-0009),
        # so a redelivered signal dedups across a crash too.
        self._cache = cache
        # The economic sibling of the Cache, written on the fill path below. It
        # is a constructor argument rather than a subscriber on purpose: the
        # manager is the projection's single writer, so a fill is applied exactly
        # once and the two read-models move together (ADR-0035).
        self._portfolio = portfolio
        # The pre-trade boundary run before any send (ADR-0017). Defaults to the
        # passthrough guard so the paper/test path needs no policy wired in.
        self._guard = guard if guard is not None else NoopGuard()

    async def on_signal(self, signal: Signal) -> None:
        # Signal handling binds the ``signal_id`` for the span of the work
        # (ADR-0020): every record emitted while placing or cancelling rides it,
        # with no call site repeating it as a field.
        with operation(signal_id=signal.signal_id):
            if isinstance(signal, PlaceSignal):
                await self._place(signal)
            elif isinstance(signal, CancelSignal):
                await self._cancel(signal)

    async def on_execution_report(self, report: ExecutionReport) -> None:
        # Venue-fact (and reconciler-synthetic) handling is saga handling: it
        # binds the ``cloid`` so every canonical transition it drives is
        # traceable to the exact order (ADR-0020).
        with operation(cloid=report.cloid):
            if isinstance(report, FillReport):
                await self._apply_fill(report)
            elif isinstance(report, OrderStatusReport):
                await self._apply_status(report)

    async def _place(self, signal: PlaceSignal) -> None:
        cloid = derive_cloid(signal.signal_id)
        if self._cache.get_order(cloid) is not None:
            # Re-seen signal_id: the saga already exists — possibly from a life
            # before a crash — never place twice (ADR-0006).
            return

        # The pre-trade boundary runs before write-ahead (ADR-0017): a denial is
        # never sent, so there is no in-flight intent to protect.
        decision = self._guard.check(signal)

        if isinstance(decision, Denied):
            # DENIED (ADR-0010): a durable terminal that was never sent. No
            # OrderPlaced/Submitted — the order goes straight from intent to
            # refused, and a re-emitted signal_id dedups against this record.
            order = self._order_for(signal, quantity=signal.quantity)
            denied = self._denied_event(order, decision.reason)
            order.apply(denied)
            self._checkpoint(order)
            await self._announce(denied)
            return

        # The quantized quantity is what actually gets placed and filled, so the
        # saga records it, not the strategy's raw intent.
        order = self._order_for(signal, quantity=decision.quantity)

        # PENDING is the write-ahead intent: durable before anything else
        # happens (ADR-0008 rule 1), then announced.
        self._checkpoint(order)
        await self._announce(self._event(OrderPlaced, order))

        submitted = self._event(OrderSubmitted, order)
        order.apply(submitted)
        await self._announce(submitted)

        await self._exchange.place(
            PlaceOrder(
                cloid=cloid,
                symbol=signal.symbol,
                side=signal.side,
                quantity=order.quantity,
                order_type=signal.order_type,
                time_in_force=signal.time_in_force,
                price=decision.price,
                post_only=signal.post_only,
            )
        )
        # SUBMITTED is checkpointed only after the send returns: until then the
        # durable record stays PENDING, so a crash anywhere in the send window
        # recovers through reconcile-by-cloid (ADR-0008 rule 2). Its timestamp
        # arms the in-flight grace clock.
        self._checkpoint(order)

    @staticmethod
    def _order_for(signal: PlaceSignal, *, quantity: Decimal) -> Order:
        """Build the saga record from a ``PlaceSignal`` at a given ``quantity`` —
        the quantized size for a placed order, the raw intent for a denial."""
        return Order(
            cloid=derive_cloid(signal.signal_id),
            strategy_id=signal.strategy_id,
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            side=signal.side,
            quantity=quantity,
            order_type=signal.order_type,
        )

    async def _cancel(self, signal: CancelSignal) -> None:
        # Re-derive the target cloid from the signal_id the strategy emitted, so
        # the cloid derivation never leaks into strategy code (ADR-0006/0026).
        cloid = derive_cloid(signal.target_signal_id)
        order = self._cache.get_order(cloid)
        if order is None:
            return  # A cancel for an order we do not own: benign no-op (ADR-0026).

        if not order.request_cancel(signal_id=signal.signal_id, ts_ns=self._clock.timestamp_ns()):
            # Already terminal, or already requested: an idempotent no-op. A
            # re-emitted CancelSignal must not send a second cancel.
            return

        # The cancel_requested marker is durable *before* the send: a crash in the
        # send window still lets reconciliation resolve an ack-lost cancel. The
        # saga stays in its current state — the marker is metadata, not a state,
        # so the order can still fill (ADR-0026).
        self._checkpoint(order)
        await self._exchange.cancel(cloid)

    async def _apply_status(self, report: OrderStatusReport) -> None:
        order = self._cache.get_order(report.cloid)
        if order is None:
            return  # A status for an order we do not own — reconciliation's concern.
        if order.is_terminal:
            # A late or synthetic status after the saga already resolved — e.g.
            # the cancel-ack that lost the race to a fill, or a reconciler
            # verdict crossing a venue push. Terminal is terminal: absorb it
            # as an idempotent no-op, never route it into ``Order.apply`` to
            # raise on an illegal transition (ADR-0026).
            return

        event = self._status_event(order, report)
        if event is None:
            return  # A status that maps to no saga transition: nothing to do.
        if not order.apply(event):
            return  # Redelivered status the saga already reflects: suppress republish.
        await self._commit(order, event)

    async def _apply_fill(self, report: FillReport) -> None:
        order = self._cache.get_order(report.cloid)
        if order is None:
            return  # A report for an order we do not own — reconciliation's concern.

        now = self._clock.timestamp_ns()
        # Order owns the fill accounting: it accumulates cum_qty, picks
        # OrderFilled vs OrderPartiallyFilled, and dedups the trade_id.
        #
        # A fill is the one saga family whose fact originates at the venue, not
        # here: ts_event carries the report's venue fill instant (when the fill
        # occurred), while ts_init is the engine's construction time (when we
        # minted the canonical event). Contrast the order-event families, which
        # correctly stamp both with `now` because the engine *is* where those
        # facts occur (ADR-0005; Event contract in domain/events.py).
        event = order.record_fill(
            trade_id=report.trade_id,
            quantity=report.quantity,
            price=report.price,
            ts_event=report.ts_event,
            ts_init=now,
            reconciliation=report.reconciliation,
        )
        if event is None:
            # Redelivered fill: the saga already reflects it. Suppress the
            # duplicate publish so downstream consumers never double-count.
            return
        # The ledger moves on the fill-apply path, strictly before the fill is
        # published — so a strategy reading its position from the OrderFilled
        # handler reads the state *this* fill produced, never a stale one
        # (ADR-0035, ADR-0045 §1). The side rides the saga because the event
        # carries the trade and the order carries the direction.
        self._portfolio.apply_fill(event, side=order.side)
        await self._commit(order, event)

    async def _commit(self, order: Order, event: OrderEvent) -> None:
        """Durably record the advanced saga, then announce it — never the reverse.

        The crash-safe tail every venue-fact handler shares: checkpoint strictly
        before publish (ADR-0008), so a crash between the two can never leave an
        announced transition the durable store never recorded. Callers apply and
        dedup first, so a deduped redelivery never reaches here — this only ever
        commits a transition that advanced the saga. ``_place`` is deliberately
        not routed through it: its ``SUBMITTED`` publish precedes the post-send
        checkpoint (ADR-0008 rule 2), the one place that ordering is inverted.
        """
        self._checkpoint(order)
        await self._announce(event)

    async def _announce(self, event: OrderEvent) -> None:
        """Emit the saga transition's named event, then publish it (ADR-0020).

        The single retrofit point for saga observability: every canonical
        ``OrderEvent`` this manager publishes is first announced under its
        cataloged name, so no state-affecting transition is silent. Correlation
        ids ride the ambient context — the ``signal_id`` while a signal is
        handled, the ``cloid`` while a venue fact is — never repeated here; only
        a terminal's ``reason``, which no correlation scope carries, is attached.
        """
        reason = getattr(event, "reason", None)
        named_event(_SAGA_EVENTS[type(event)], **({"reason": reason} if reason else {}))
        await self._bus.publish(event)

    def _checkpoint(self, order: Order) -> None:
        try:
            self._cache.checkpoint(order, ts_ns=self._clock.timestamp_ns())
        except Exception as exc:
            # A checkpoint the store cannot make durable is a broken engine
            # assumption (ADR-0014): fail fast rather than run a saga whose
            # memory and durable states silently diverge.
            raise InvariantViolation(
                f"checkpoint write failed for cloid {order.cloid} in state {order.state.value}"
            ) from exc

    def _event[E: (OrderPlaced, OrderSubmitted, OrderLive, OrderCancelled)](
        self,
        event_type: type[E],
        order: Order,
        *,
        venue_oid: str | None = None,
        reconciliation: bool = False,
    ) -> E:
        """Build a reason-less canonical ``OrderEvent`` from ``order``'s identity.

        The one home for the shared saga-event envelope (cloid, strategy id,
        signal id, symbol, venue oid, timestamps). ``venue_oid`` defaults to the
        order's own; a venue status carrying a fresher one passes it in. The
        constraint set is exactly the reason-less families — ``OrderRejected``
        and the other terminal-with-reason events are built explicitly, so mypy
        rejects routing them through here without their required ``reason``.
        """
        now = self._clock.timestamp_ns()
        return event_type(
            ts_event=now,
            ts_init=now,
            cloid=order.cloid,
            strategy_id=order.strategy_id,
            signal_id=order.signal_id,
            symbol=order.symbol,
            venue_oid=venue_oid if venue_oid is not None else order.venue_oid,
            reconciliation=reconciliation,
        )

    def _denied_event(self, order: Order, reason: str) -> OrderDenied:
        """Build the ``DENIED`` terminal from ``order``'s identity (ADR-0010).

        The guard's pre-trade refusal, minted here rather than by a venue fact —
        ``DENIED`` is the one terminal the venue never sees."""
        now = self._clock.timestamp_ns()
        return OrderDenied(
            ts_event=now,
            ts_init=now,
            cloid=order.cloid,
            strategy_id=order.strategy_id,
            signal_id=order.signal_id,
            symbol=order.symbol,
            venue_oid=order.venue_oid,
            reason=reason,
        )

    def _status_event(self, order: Order, report: OrderStatusReport) -> OrderEvent | None:
        """Translate a raw ``OrderStatusReport`` into the canonical transition, or
        ``None`` for a status that maps to no saga move. A fresher ``venue_oid`` on
        the report wins; otherwise the order's own is kept. The report's
        ``reconciliation`` provenance carries through (ADR-0011 inv 6).

        ``SUBMITTED`` and ``FAILED`` are minted only by the ``Reconciler``:
        the bridge for a recovered ``PENDING`` whose send provably landed, and
        the proven-never-landed verdict (ADR-0010) — verdicts, not venue pushes.
        """
        oid, flag = report.venue_oid, report.reconciliation
        match report.status:
            case OrderState.SUBMITTED:
                return self._event(OrderSubmitted, order, venue_oid=oid, reconciliation=flag)
            case OrderState.LIVE:
                return self._event(OrderLive, order, venue_oid=oid, reconciliation=flag)
            case OrderState.CANCELLED:
                return self._event(OrderCancelled, order, venue_oid=oid, reconciliation=flag)
            case OrderState.REJECTED:
                return self._reasoned_event(OrderRejected, order, report, "venue rejected")
            case OrderState.FAILED:
                return self._reasoned_event(OrderFailed, order, report, "proven never landed")
            case _:
                return None

    def _reasoned_event[E: (OrderRejected, OrderFailed)](
        self,
        event_type: type[E],
        order: Order,
        report: OrderStatusReport,
        default_reason: str,
    ) -> E:
        """Build a terminal-with-reason event from ``order``'s identity and the
        report's adjudication — the required-``reason`` twin of ``_event``."""
        now = self._clock.timestamp_ns()
        return event_type(
            ts_event=now,
            ts_init=now,
            cloid=order.cloid,
            strategy_id=order.strategy_id,
            signal_id=order.signal_id,
            symbol=order.symbol,
            venue_oid=report.venue_oid if report.venue_oid is not None else order.venue_oid,
            reason=report.reason or default_reason,
            reconciliation=report.reconciliation,
        )
