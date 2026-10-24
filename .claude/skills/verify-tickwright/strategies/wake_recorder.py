"""A user strategy that records its wake-ups, for the strategy wake recipe (issue #557).

Config names it as ``wake_recorder:WakeRecorder``. It trades nothing. It wakes every
second and keeps the slot of every ``on_wake`` in its snapshot. The engine saves the
snapshot after each callback, so the store shows every slot that reached the strategy,
across a crash too (ADR-0062).
"""

import json

from pydantic import BaseModel

from tickwright.domain import MarketTick, OrderEvent, Portfolio, SignalEmitter, Wake


class WakeRecorder:
    class Settings(BaseModel):
        pass

    wake_interval_seconds: float | None = 1.0

    def __init__(self, *, emitter: SignalEmitter, portfolio: Portfolio, settings: Settings) -> None:
        del portfolio, settings
        self.strategy_id = emitter.strategy_id
        self._emitter = emitter
        self._slots: list[int] = []

    async def on_tick(self, tick: MarketTick) -> None:
        pass

    async def on_wake(self, wake: Wake) -> None:
        self._slots.append(wake.ts_event)

    async def on_order_event(self, event: OrderEvent) -> None:
        pass

    def set_next_seq(self, next_seq: int) -> None:
        self._emitter.set_next_seq(next_seq)

    def snapshot(self) -> bytes:
        return json.dumps({"slots": self._slots}).encode()

    def restore(self, data: bytes) -> None:
        # Kept, so the list shows the slots of both lives in the order they ran.
        self._slots = json.loads(data)["slots"]
