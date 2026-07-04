"""``SingleShotLimitStrategy`` — the resting-LIMIT + cancel reference ``Strategy``
(ADR-0016, issue #13).

Proves the seam across the second order shape and the cancel path: on its first
tick it emits exactly one LIMIT ``PlaceSignal``; an optional ``cancel_after_ticks``
makes it emit exactly one ``CancelSignal`` after that many later ticks, targeting
its own order **by the ``signal_id`` it emitted** — the strategy never derives or
sees a ``cloid`` (ADR-0006/0026). It records the ``OrderFilled`` and
``OrderCancelled`` it receives. It owns its monotonic ``seq`` so both signals are
deterministic and replayable, and knows nothing of the saga, store, or venue.
"""

from decimal import Decimal

from tickwright.domain import (
    CancelSignal,
    Clock,
    EventBus,
    InvariantViolation,
    MarketTick,
    OrderCancelled,
    OrderEvent,
    OrderFilled,
    OrderType,
    PlaceSignal,
    Side,
    TimeInForce,
)


class SingleShotLimitStrategy:
    """Places one resting LIMIT on the first tick; optionally cancels it later."""

    def __init__(
        self,
        *,
        strategy_id: str,
        bus: EventBus,
        clock: Clock,
        side: Side,
        quantity: Decimal,
        price: Decimal,
        time_in_force: TimeInForce = TimeInForce.GTC,
        post_only: bool = False,
        cancel_after_ticks: int | None = None,
    ) -> None:
        self.strategy_id = strategy_id
        self._bus = bus
        self._clock = clock
        self._side = side
        self._quantity = quantity
        self._price = price
        self._time_in_force = time_in_force
        self._post_only = post_only
        self._cancel_after_ticks = cancel_after_ticks
        self._seq = 0
        self._placed_signal_id: str | None = None
        self._ticks_since_place = 0
        self._cancelled = False
        self.fills: list[OrderFilled] = []
        self.cancellations: list[OrderCancelled] = []

    async def on_tick(self, tick: MarketTick) -> None:
        if self._placed_signal_id is None:
            await self._place(tick)
            return
        await self._maybe_cancel(tick)

    async def _place(self, tick: MarketTick) -> None:
        self._seq += 1
        now = self._clock.timestamp_ns()
        signal = PlaceSignal(
            ts_event=now,
            ts_init=now,
            strategy_id=self.strategy_id,
            symbol=tick.symbol,
            seq=self._seq,
            side=self._side,
            quantity=self._quantity,
            order_type=OrderType.LIMIT,
            time_in_force=self._time_in_force,
            price=self._price,
            post_only=self._post_only,
        )
        self._placed_signal_id = signal.signal_id
        await self._bus.publish(signal)

    async def _maybe_cancel(self, tick: MarketTick) -> None:
        if self._cancel_after_ticks is None or self._cancelled:
            return
        if self._placed_signal_id is None:
            # on_tick only routes here after a place; a missing id is a broken assumption.
            raise InvariantViolation("cancel path reached before a signal was placed")
        self._ticks_since_place += 1
        if self._ticks_since_place < self._cancel_after_ticks:
            return
        self._cancelled = True
        self._seq += 1
        now = self._clock.timestamp_ns()
        await self._bus.publish(
            CancelSignal(
                ts_event=now,
                ts_init=now,
                strategy_id=self.strategy_id,
                symbol=tick.symbol,
                seq=self._seq,
                target_signal_id=self._placed_signal_id,
            )
        )

    async def on_order_event(self, event: OrderEvent) -> None:
        if isinstance(event, OrderFilled):
            self.fills.append(event)
        elif isinstance(event, OrderCancelled):
            self.cancellations.append(event)
