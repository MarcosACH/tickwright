"""``SingleShotMarketStrategy`` — the minimal reference ``Strategy`` (ADR-0016).

Emits exactly one MARKET order on its first tick, then stays quiet, and records
the ``OrderFilled`` it receives. It owns its monotonic ``seq`` (so ``signal_id`` is
deterministic and replayable, ADR-0006) and knows nothing of the saga, the store,
or the venue — the whole point of the surrounding engine is that a strategy stays
this small. Depends on ``domain`` only; it emits no named events itself.
"""

import json
from decimal import Decimal

from tickwright.domain import (
    Clock,
    EventBus,
    MarketTick,
    OrderEvent,
    OrderFilled,
    OrderType,
    Side,
    TimeInForce,
)

from .emitter import SignalEmitter


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
        self._emitter = SignalEmitter(strategy_id=strategy_id, bus=bus, clock=clock)
        self._side = side
        self._quantity = quantity
        self._time_in_force = time_in_force
        # The emitter owns the seq counter (engine-set from the saga high-water,
        # ADR-0016); the fired flag is the snapshot content this strategy owns.
        self._fired = False
        self.fills: list[OrderFilled] = []

    async def on_tick(self, tick: MarketTick) -> None:
        if self._fired:
            return  # Single shot: one order, on the first tick only.
        self._fired = True
        await self._emitter.place(
            symbol=tick.symbol,
            side=self._side,
            quantity=self._quantity,
            order_type=OrderType.MARKET,
            time_in_force=self._time_in_force,
        )

    async def on_order_event(self, event: OrderEvent) -> None:
        if isinstance(event, OrderFilled):
            self.fills.append(event)

    def set_next_seq(self, next_seq: int) -> None:
        """Resume the seq counter from the engine-recovered high-water (ADR-0016)."""
        self._emitter.set_next_seq(next_seq)

    def snapshot(self) -> bytes:
        """State content only — the seq never travels in the snapshot."""
        return json.dumps({"version": 1, "fired": self._fired}).encode()

    def restore(self, data: bytes) -> None:
        """Rebuild from ``snapshot()`` bytes; raises on an unknown shape, which
        the engine treats as start-fresh (ADR-0016)."""
        state = json.loads(data)
        if state.get("version") != 1:
            raise ValueError(f"unknown snapshot version: {state.get('version')!r}")
        self._fired = bool(state["fired"])
