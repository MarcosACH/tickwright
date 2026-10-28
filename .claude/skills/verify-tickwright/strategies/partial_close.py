"""A user strategy outside the package, for the testnet partial close recipe (issue #591).

Config names the class as ``partial_close:PartialClose`` (ADR-0061).

It buys 2 x quantity, holds, sells quantity, holds, and sells the rest. Each hold is wall time
from the fill. It must outlast the 60 second reconcile cadence, so a reconcile runs while half
the position is open. That is the state where a venue that rounds its cost basis on a partial
fill once made the cash check heal (#582). See features/hyperliquid-testnet.md.
"""

import json
import time
from decimal import Decimal

import structlog
from pydantic import BaseModel

from tickwright.domain import (
    MarketTick,
    OrderEvent,
    OrderFilled,
    OrderType,
    Portfolio,
    Side,
    SignalEmitter,
    TimeInForce,
    Wake,
)

log = structlog.get_logger()

# The phase after each fill. A fill in any other phase changes nothing.
_AFTER_FILL = {"opening": "held_full", "partial_closing": "held_half", "closing": "flat"}


class PartialClose:
    class Settings(BaseModel):
        quantity: Decimal
        # A buy sent before the first mark is never sent (#509), so wait for one.
        wait_seconds: float
        hold_seconds: float

    wake_interval_seconds: float | None = None

    def __init__(self, *, emitter: SignalEmitter, portfolio: Portfolio, settings: Settings) -> None:
        self.strategy_id = emitter.strategy_id
        self._emitter = emitter
        self._portfolio = portfolio
        self._quantity = settings.quantity
        self._wait_seconds = settings.wait_seconds
        self._hold_seconds = settings.hold_seconds
        self._phase = "idle"
        self._since: float | None = None

    def _elapsed(self, seconds: float) -> bool:
        """Whether ``seconds`` of wall time have passed since the phase began.

        Wall time, not trade time: the testnet feed conflates, so trade time can jump.
        """
        if self._since is None:
            self._since = time.monotonic()
        return time.monotonic() - self._since >= seconds

    async def _market(self, symbol: str, side: Side, quantity: Decimal) -> None:
        await self._emitter.place(
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.IOC,
        )

    async def on_tick(self, tick: MarketTick) -> None:
        if self._phase == "idle" and self._elapsed(self._wait_seconds):
            self._phase = "opening"
            await self._market(tick.symbol, Side.BUY, 2 * self._quantity)
        elif self._phase == "held_full" and self._elapsed(self._hold_seconds):
            self._phase = "partial_closing"
            await self._market(tick.symbol, Side.SELL, self._quantity)
        elif self._phase == "held_half" and self._elapsed(self._hold_seconds):
            self._phase = "closing"
            await self._market(tick.symbol, Side.SELL, self._quantity)

    async def on_wake(self, wake: Wake) -> None:
        pass

    async def on_order_event(self, event: OrderEvent) -> None:
        if not isinstance(event, OrderFilled) or self._phase not in _AFTER_FILL:
            return
        self._phase = _AFTER_FILL[self._phase]
        self._since = None
        position = self._portfolio.position(event.symbol)
        log.info(
            "verify.flat" if self._phase == "flat" else "verify.fill",
            strategy_id=self.strategy_id,
            phase=self._phase,
            cloid=event.cloid,
            size=str(position.size) if position is not None else None,
        )

    def set_next_seq(self, next_seq: int) -> None:
        self._emitter.set_next_seq(next_seq)

    def snapshot(self) -> bytes:
        return json.dumps({"phase": self._phase}).encode()

    def restore(self, data: bytes) -> None:
        # The recipe never restarts this life. A restart resumes the phase, and the hold
        # starts again from the restart.
        self._phase = json.loads(data)["phase"]
