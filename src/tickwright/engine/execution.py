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
    OrderLive,
    OrderPlaced,
    OrderRejected,
    OrderState,
    OrderStatusReport,
    OrderSubmitted,
    PlaceOrder,
    PlaceSignal,
    Signal,
    Store,
    derive_cloid,
)


class ExecutionManager:
    """Owns cloid assignment, the saga FSM, and canonical ``OrderEvent`` publishing."""

    def __init__(self, *, bus: EventBus, clock: Clock, exchange: Exchange, store: Store) -> None:
        self._bus = bus
        self._clock = clock
        self._exchange = exchange
        self._store = store
        self._orders: dict[str, Order] = {}  # working set; the Store is the durable copy.

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
        if cloid in self._orders:
            # Re-seen signal_id: resume the existing saga, never place twice (ADR-0006).
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
        self._orders[cloid] = order

        # PENDING is the write-ahead intent: durable before anything else
        # happens (ADR-0008 rule 1), then announced.
        self._checkpoint(order)
        await self._bus.publish(self._order_event(OrderPlaced, order))

        submitted = self._order_event(OrderSubmitted, order)
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
        order = self._orders.get(cloid)
        if order is None:
            return  # A cancel for an order we do not own: benign no-op (ADR-0026).

        if not order.request_cancel(ts_ns=self._clock.timestamp_ns()):
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
        order = self._orders.get(report.cloid)
        if order is None:
            return  # A status for an order we do not own — reconciliation's concern.

        event = self._status_event(order, report)
        if event is None:
            return  # A status that maps to no saga transition: nothing to do.
        if not order.apply(event):
            return  # Redelivered status the saga already reflects: suppress republish.
        self._checkpoint(order)
        await self._bus.publish(event)

    async def _apply_fill(self, report: FillReport) -> None:
        order = self._orders.get(report.cloid)
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
        )
        if event is None:
            # Redelivered fill: the saga already reflects it. Suppress the
            # duplicate publish so downstream consumers never double-count.
            return
        self._checkpoint(order)
        await self._bus.publish(event)

    def _checkpoint(self, order: Order) -> None:
        try:
            self._store.checkpoint(order, ts_ns=self._clock.timestamp_ns())
        except Exception as exc:
            # A checkpoint the store cannot make durable is a broken engine
            # assumption (ADR-0014): fail fast rather than run a saga whose
            # memory and durable states silently diverge.
            raise InvariantViolation(
                f"checkpoint write failed for cloid {order.cloid} in state {order.state.value}"
            ) from exc

    def _order_event[E: (OrderPlaced, OrderSubmitted)](
        self, event_type: type[E], order: Order
    ) -> E:
        now = self._clock.timestamp_ns()
        return event_type(
            ts_event=now,
            ts_init=now,
            cloid=order.cloid,
            strategy_id=order.strategy_id,
            signal_id=order.signal_id,
            symbol=order.symbol,
            venue_oid=order.venue_oid,
        )

    def _status_event(self, order: Order, report: OrderStatusReport) -> OrderEvent | None:
        """Translate a raw ``OrderStatusReport`` into the canonical transition, or
        ``None`` for a status that maps to no saga move."""
        now = self._clock.timestamp_ns()
        venue_oid = report.venue_oid if report.venue_oid is not None else order.venue_oid
        match report.status:
            case OrderState.LIVE:
                return OrderLive(
                    ts_event=now,
                    ts_init=now,
                    cloid=order.cloid,
                    strategy_id=order.strategy_id,
                    signal_id=order.signal_id,
                    symbol=order.symbol,
                    venue_oid=venue_oid,
                )
            case OrderState.CANCELLED:
                return OrderCancelled(
                    ts_event=now,
                    ts_init=now,
                    cloid=order.cloid,
                    strategy_id=order.strategy_id,
                    signal_id=order.signal_id,
                    symbol=order.symbol,
                    venue_oid=venue_oid,
                )
            case OrderState.REJECTED:
                return OrderRejected(
                    ts_event=now,
                    ts_init=now,
                    cloid=order.cloid,
                    strategy_id=order.strategy_id,
                    signal_id=order.signal_id,
                    symbol=order.symbol,
                    venue_oid=venue_oid,
                    reason=report.reason or "venue rejected",
                )
            case _:
                return None
