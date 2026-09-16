"""RoundTripLoopStrategy — a throwaway demo Strategy, not a shipped `kind`.

The `Strategy` seam already ships its two impls (single_shot_market,
single_shot_limit) and docs/extending.md is explicit that a third stays out
of tree rather than growing AppConfig's `kind` Literal. This is that third
one: it satisfies the `Strategy` Protocol by shape and is registered by hand
in run_live_paper.py instead of through StrategyConfig.

Behavior: on the first tick, starts one sequential loop — buy, wait
`hold_seconds`, sell, wait `cycle_seconds`, repeat. One round trip at a time,
never overlapping. Reads no Portfolio (nothing here needs one) and keeps no
state worth resuming across a restart, so restore() always starts fresh.
"""

import asyncio
from decimal import Decimal

from tickwright.domain import Clock, EventBus, MarketTick, OrderEvent, OrderType, Side, TimeInForce
from tickwright.strategies import SignalEmitter


class RoundTripLoopStrategy:
    def __init__(
        self,
        *,
        strategy_id: str,
        bus: EventBus,
        clock: Clock,
        symbol: str,
        quantity: Decimal,
        hold_seconds: float,
        cycle_seconds: float,
    ) -> None:
        self.strategy_id = strategy_id
        self._emitter = SignalEmitter(strategy_id=strategy_id, bus=bus, clock=clock)
        self._clock = clock
        self._symbol = symbol
        self._quantity = quantity
        self._hold_seconds = hold_seconds
        self._cycle_seconds = cycle_seconds
        self._started = False
        # Held so the loop task is never garbage-collected mid-flight.
        self._loop_task: asyncio.Task[None] | None = None

    async def on_tick(self, tick: MarketTick) -> None:
        if self._started:
            return
        self._started = True
        self._loop_task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            await self._emitter.place(
                symbol=self._symbol,
                side=Side.BUY,
                quantity=self._quantity,
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.IOC,
            )
            await self._clock.sleep(self._hold_seconds)
            await self._emitter.place(
                symbol=self._symbol,
                side=Side.SELL,
                quantity=self._quantity,
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.IOC,
            )
            await self._clock.sleep(self._cycle_seconds)

    async def on_order_event(self, event: OrderEvent) -> None:
        return  # Nothing read: the engine's own order.* logs are the point.

    def set_next_seq(self, next_seq: int) -> None:
        self._emitter.set_next_seq(next_seq)

    def snapshot(self) -> bytes:
        return b'{"version": 1}'

    def restore(self, data: bytes) -> None:
        return  # A timer-driven demo has nothing worth resuming.
