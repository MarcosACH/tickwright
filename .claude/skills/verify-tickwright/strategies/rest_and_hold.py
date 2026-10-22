"""A user strategy outside the package, for the container restart recipe (issue #534).

A container life mounts this directory at ``/strategies``. Config names the class as
``rest_and_hold:RestAndHold`` (ADR-0061).

On its first tick it buys at market to open a position. It also rests a buy limit
below the market. After a restart it rebuilds both facts from ``position()`` and
``open_orders()``, not from its snapshot. The startup barrier can settle an order
before the strategy subscribes, so a snapshot can be stale (ADR-0041 §3).
"""

import json
from decimal import Decimal

import structlog
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

log = structlog.get_logger()


class RestAndHold:
    class Settings(BaseModel):
        symbol: str
        quantity: Decimal
        rest_price: Decimal

    def __init__(self, *, emitter: SignalEmitter, portfolio: Portfolio, settings: Settings) -> None:
        self.strategy_id = emitter.strategy_id
        self._emitter = emitter
        self._portfolio = portfolio
        self._symbol = settings.symbol
        self._quantity = settings.quantity
        self._rest_price = settings.rest_price
        self._opened = False
        self._rested = False

    async def on_tick(self, tick: MarketTick) -> None:
        if tick.symbol != self._symbol:
            return
        if not self._opened:
            self._opened = True
            await self._emitter.place(
                symbol=self._symbol,
                side=Side.BUY,
                quantity=self._quantity,
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.IOC,
            )
        if not self._rested:
            self._rested = True
            await self._emitter.place(
                symbol=self._symbol,
                side=Side.BUY,
                quantity=self._quantity,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.GTC,
                price=self._rest_price,
            )

    async def on_order_event(self, event: OrderEvent) -> None:
        pass

    def set_next_seq(self, next_seq: int) -> None:
        self._emitter.set_next_seq(next_seq)

    def snapshot(self) -> bytes:
        return json.dumps({"opened": self._opened, "rested": self._rested}).encode()

    def restore(self, data: bytes) -> None:
        # The snapshot is ignored on purpose. This strategy proves the reads alone are enough.
        del data
        position = self._portfolio.position(self._symbol)
        orders = self._portfolio.open_orders(self._symbol)
        size = position.size if position is not None else Decimal(0)
        # A market order still pending at the crash counts as opened.
        self._opened = size != 0 or any(o.order_type is OrderType.MARKET for o in orders)
        self._rested = any(o.order_type is OrderType.LIMIT for o in orders)
        log.info(
            "verify.restored",
            strategy_id=self.strategy_id,
            position_size=str(size),
            open_orders=[o.signal_id for o in orders],
            opened=self._opened,
            rested=self._rested,
        )
