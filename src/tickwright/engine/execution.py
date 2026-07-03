"""``ExecutionManager`` — the single engine-internal saga orchestrator (ADR-0015).

Deliberately *not* a Protocol: there is one of it, and it owns the crash-safe saga
once so Paper and Hyperliquid are served identically. It subscribes to ``Signal``s
and raw ``ExecutionReport``s, assigns the ``cloid`` from the ``signal_id``, drives
``Order.apply``, and publishes the canonical ``OrderEvent``s strategies consume.

This is the happy-path manager for the tracer: derive cloid, record ``PENDING``,
publish ``OrderPlaced`` → ``OrderSubmitted`` while sending to the exchange, then on
the ``FillReport`` publish ``OrderFilled``. The write-ahead ``Store`` checkpoint,
the ``PreTradeGuard``, cancels, and the partial/terminal transitions are later
slices — this one keeps the saga in memory to prove the pipeline end to end.
"""

from tickwright.domain import (
    Clock,
    EventBus,
    Exchange,
    ExecutionReport,
    FillReport,
    Order,
    OrderFilled,
    OrderPlaced,
    OrderSubmitted,
    PlaceOrder,
    PlaceSignal,
    Signal,
    derive_cloid,
)


class ExecutionManager:
    """Owns cloid assignment, the saga FSM, and canonical ``OrderEvent`` publishing."""

    def __init__(self, *, bus: EventBus, clock: Clock, exchange: Exchange) -> None:
        self._bus = bus
        self._clock = clock
        self._exchange = exchange
        self._orders: dict[str, Order] = {}  # in-memory saga; the Store lands later.

    async def on_signal(self, signal: Signal) -> None:
        if isinstance(signal, PlaceSignal):
            await self._place(signal)
        # CancelSignal handling arrives with the cancel slice.

    async def on_execution_report(self, report: ExecutionReport) -> None:
        if isinstance(report, FillReport):
            await self._apply_fill(report)

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

        # PENDING is the write-ahead intent; announce it before the send.
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

    async def _apply_fill(self, report: FillReport) -> None:
        order = self._orders.get(report.cloid)
        if order is None:
            return  # A report for an order we do not own — reconciliation's concern.

        cum_qty = order.cum_qty + report.quantity
        now = self._clock.timestamp_ns()
        filled = OrderFilled(
            ts_event=now,
            ts_init=now,
            cloid=order.cloid,
            strategy_id=order.strategy_id,
            signal_id=order.signal_id,
            symbol=order.symbol,
            venue_oid=order.venue_oid,
            trade_id=report.trade_id,
            quantity=report.quantity,
            price=report.price,
            cum_qty=cum_qty,
        )
        if not order.apply(filled):
            # Redelivered fill: the saga already reflects it. Suppress the
            # duplicate publish so downstream consumers never double-count.
            return
        await self._bus.publish(filled)

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
