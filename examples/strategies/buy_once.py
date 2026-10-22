"""An example user strategy that lives outside the tickwright package.

The container image puts ``/strategies`` on the import path. Mount this directory
there and name the class in config as ``buy_once:BuyOnce`` (ADR-0061).
It buys a fixed quantity at market on the first tick, then does nothing.
"""

import json
from decimal import Decimal

from pydantic import BaseModel

from tickwright.domain import (
    MarketTick,
    OrderEvent,
    OrderType,
    Portfolio,
    Side,
    SignalEmitter,
    TimeInForce,
)


class BuyOnce:
    class Settings(BaseModel):
        quantity: Decimal

    def __init__(self, *, emitter: SignalEmitter, portfolio: Portfolio, settings: Settings) -> None:
        self.strategy_id = emitter.strategy_id
        self._emitter = emitter
        self._quantity = settings.quantity
        self._fired = False

    async def on_tick(self, tick: MarketTick) -> None:
        if self._fired:
            return
        self._fired = True
        await self._emitter.place(
            symbol=tick.symbol,
            side=Side.BUY,
            quantity=self._quantity,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.IOC,
        )

    async def on_order_event(self, event: OrderEvent) -> None:
        pass

    def set_next_seq(self, next_seq: int) -> None:
        self._emitter.set_next_seq(next_seq)

    def snapshot(self) -> bytes:
        # Saved so a restart does not buy a second time.
        return json.dumps({"fired": self._fired}).encode()

    def restore(self, data: bytes) -> None:
        self._fired = bool(json.loads(data)["fired"])
