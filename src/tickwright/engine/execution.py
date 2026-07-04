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
(later) reconciliation move an order.

A ``CancelSignal`` re-derives the target ``cloid`` from ``target_signal_id``,
durably checkpoints the ``cancel_requested`` marker **before** calling
``Exchange.cancel``, and leaves the saga ``LIVE`` — the marker is metadata, not a
state, so the order can still fill (ADR-0026). The ``PreTradeGuard`` is a later
slice.
"""

from tickwright.domain import (
    CancelSignal,
    Clock,
    EventBus,
    Exchange,
    ExecutionReport,
    FillReport,
    InvariantViolation,
    Order,
    OrderCancelled,
    OrderEvent,
    OrderFailed,
    OrderLive,
    OrderPlaced,
    OrderRejected,
    OrderState,
    OrderStatusReport,
    OrderSubmitted,
    PlaceOrder,
    PlaceSignal,
    Signal,
    derive_cloid,
)

from .cache import Cache


class ExecutionManager:
    """Owns cloid assignment, the saga FSM, and canonical ``OrderEvent`` publishing."""

    def __init__(self, *, bus: EventBus, clock: Clock, exchange: Exchange, cache: Cache) -> None:
        self._bus = bus
        self._clock = clock
        self._exchange = exchange
        # The working set and the durable copy in one seam: the Cache projects
        # every checkpoint and is rebuilt from the Store on restart (ADR-0009),
        # so a redelivered signal dedups across a crash too.
        self._cache = cache

    async def on_signal(self, signal: Signal) -> None:
        if isinstance(signal, PlaceSignal):
            await self._place(signal)
        elif isinstance(signal, CancelSignal):
            await self._cancel(signal)

    async def on_execution_report(self, report: ExecutionReport) -> None:
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

        order = Order(
            cloid=cloid,
            strategy_id=signal.strategy_id,
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            side=signal.side,
            quantity=signal.quantity,
            order_type=signal.order_type,
        )

        # PENDING is the write-ahead intent: durable before anything else
        # happens (ADR-0008 rule 1), then announced.
        self._checkpoint(order)
        await self._bus.publish(self._event(OrderPlaced, order))

        submitted = self._event(OrderSubmitted, order)
        order.apply(submitted)
        await self._bus.publish(submitted)

        await self._exchange.place(
            PlaceOrder(
                cloid=cloid,
                symbol=signal.symbol,
                side=signal.side,
                quantity=signal.quantity,
                order_type=signal.order_type,
                time_in_force=signal.time_in_force,
                price=signal.price,
                post_only=signal.post_only,
            )
        )
        # SUBMITTED is checkpointed only after the send returns: until then the
        # durable record stays PENDING, so a crash anywhere in the send window
        # recovers through reconcile-by-cloid (ADR-0008 rule 2). Its timestamp
        # arms the in-flight grace clock.
        self._checkpoint(order)

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
        event = order.record_fill(
            trade_id=report.trade_id,
            quantity=report.quantity,
            price=report.price,
            ts_event=now,
            ts_init=now,
            reconciliation=report.reconciliation,
        )
        if event is None:
            # Redelivered fill: the saga already reflects it. Suppress the
            # duplicate publish so downstream consumers never double-count.
            return
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

    def _status_event(self, order: Order, report: OrderStatusReport) -> OrderEvent | None:
        """Translate a raw ``OrderStatusReport`` into the canonical transition, or
        ``None`` for a status that maps to no saga move. A fresher ``venue_oid`` on
        the report wins; otherwise the order's own is kept. The report's
        ``reconciliation`` provenance carries through (ADR-0011 inv 6)."""
        match report.status:
            case OrderState.SUBMITTED:
                # Only the Reconciler mints this status, bridging a recovered
                # PENDING intent whose send provably landed at the venue.
                return self._event(
                    OrderSubmitted,
                    order,
                    venue_oid=report.venue_oid,
                    reconciliation=report.reconciliation,
                )
            case OrderState.LIVE:
                return self._event(
                    OrderLive,
                    order,
                    venue_oid=report.venue_oid,
                    reconciliation=report.reconciliation,
                )
            case OrderState.CANCELLED:
                return self._event(
                    OrderCancelled,
                    order,
                    venue_oid=report.venue_oid,
                    reconciliation=report.reconciliation,
                )
            case OrderState.REJECTED:
                now = self._clock.timestamp_ns()
                return OrderRejected(
                    ts_event=now,
                    ts_init=now,
                    cloid=order.cloid,
                    strategy_id=order.strategy_id,
                    signal_id=order.signal_id,
                    symbol=order.symbol,
                    venue_oid=report.venue_oid if report.venue_oid is not None else order.venue_oid,
                    reason=report.reason or "venue rejected",
                    reconciliation=report.reconciliation,
                )
            case OrderState.FAILED:
                # Only the Reconciler mints this status: positive proof the
                # order never landed (ADR-0010) — a verdict, not a venue push.
                now = self._clock.timestamp_ns()
                return OrderFailed(
                    ts_event=now,
                    ts_init=now,
                    cloid=order.cloid,
                    strategy_id=order.strategy_id,
                    signal_id=order.signal_id,
                    symbol=order.symbol,
                    venue_oid=order.venue_oid,
                    reason=report.reason or "proven never landed",
                    reconciliation=report.reconciliation,
                )
            case _:
                return None
