"""``PaperExchange`` — the deterministic in-process ``Exchange`` and default v1
target (ADR-0012).

A *real* exchange, never a mock: it caches the latest tick per symbol and fills a
MARKET order on receipt against that price via the injected ``FillModel``, then
emits a raw ``FillReport`` on the bus. It owns no saga — the ``ExecutionManager``
turns that raw fact into the canonical ``OrderFilled`` (ADR-0015). Frictionless in
v1: price and quantity only, no fees/margin/PnL (ADR-0013).

MARKET-only for this tracer slice; the resting-LIMIT book lands next.
"""

from tickwright.domain import (
    Clock,
    EventBus,
    FillReport,
    MarketTick,
    OrderType,
    PlaceOrder,
)

from .fill_model import Fill, FillModel


class PaperExchange:
    """An ``Exchange`` that fills against replayed/live ticks, deterministically."""

    def __init__(self, *, bus: EventBus, clock: Clock, fill_model: FillModel) -> None:
        self._bus = bus
        self._clock = clock
        self._fill_model = fill_model
        self._latest_tick: dict[str, MarketTick] = {}
        self._fill_counts: dict[str, int] = {}

    async def on_tick(self, tick: MarketTick) -> None:
        # Cache the latest price per symbol; MARKET fills read it (ADR-0027).
        self._latest_tick[tick.symbol] = tick

    async def place(self, order: PlaceOrder) -> None:
        if order.order_type is not OrderType.MARKET:
            raise NotImplementedError("PaperExchange v1 tracer is MARKET-only")

        tick = self._latest_tick.get(order.symbol)
        if tick is None:
            raise ValueError(f"no market tick cached for {order.symbol!r}; cannot fill MARKET")

        fill = self._fill_model.market_fill(order, tick)
        await self._bus.publish(self._fill_report(order, fill))

    def _fill_report(self, order: PlaceOrder, fill: Fill) -> FillReport:
        index = self._fill_counts.get(order.cloid, 0) + 1
        self._fill_counts[order.cloid] = index
        now = self._clock.timestamp_ns()
        return FillReport(
            ts_event=now,
            ts_init=now,
            cloid=order.cloid,
            symbol=order.symbol,
            trade_id=f"{order.cloid}-{index}",
            quantity=fill.quantity,
            price=fill.price,
        )
