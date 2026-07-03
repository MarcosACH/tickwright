"""``SingleShotMarketStrategy`` — the minimal reference ``Strategy`` (ADR-0016).

Emits exactly one MARKET order on its first tick, then stays quiet, and records
the ``OrderFilled`` it receives. It owns its monotonic ``seq`` (so ``signal_id`` is
deterministic and replayable, ADR-0006) and knows nothing of the saga, the store,
or the venue — the whole point of the surrounding engine is that a strategy stays
this small. Depends on ``domain`` only; it emits no named events itself.
"""

from decimal import Decimal

from tickwright.domain import (
    Clock,
    EventBus,
    MarketTick,
    OrderEvent,
    OrderFilled,
    OrderType,
    PlaceSignal,
    Side,
    TimeInForce,
)


class SingleShotMarketStrategy:
    """Buys (or sells) a fixed quantity at market, once, on the first tick seen."""

    def __init__(
        self,
        *,
        strategy_id: str,
        bus: EventBus,
        clock: Clock,
        side: Side,
        quantity: Decimal,
        time_in_force: TimeInForce = TimeInForce.IOC,
    ) -> None:
        self.strategy_id = strategy_id
        self._bus = bus
        self._clock = clock
        self._side = side
        self._quantity = quantity
        self._time_in_force = time_in_force
        self._seq = 0
        self.fills: list[OrderFilled] = []

    async def on_tick(self, tick: MarketTick) -> None:
        if self._seq > 0:
            return  # Single shot: one order, on the first tick only.
        self._seq += 1
        now = self._clock.timestamp_ns()
        await self._bus.publish(
            PlaceSignal(
                ts_event=now,
                ts_init=now,
                strategy_id=self.strategy_id,
                symbol=tick.symbol,
                seq=self._seq,
                side=self._side,
                quantity=self._quantity,
                order_type=OrderType.MARKET,
                time_in_force=self._time_in_force,
            )
        )

    async def on_order_event(self, event: OrderEvent) -> None:
        if isinstance(event, OrderFilled):
            self.fills.append(event)
