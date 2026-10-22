"""Strategies that live outside ``src/``, the way a user's own strategy does.

A test names one by import path, such as ``user_strategies:BuysEachSymbolOnce``,
and the engine imports it at boot (ADR-0061). They sit in the test tree so that
nothing in the shipped package can name them.
"""

import json
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from tickwright.domain import (
    MarketTick,
    OrderEvent,
    OrderType,
    Portfolio,
    Side,
    SignalEmitter,
    TimeInForce,
    Wake,
)


class BuysEachSymbolOnce:
    """Buys its settings quantity at market once on each symbol it trades."""

    class Settings(BaseModel):
        quantity: Decimal

    wake_interval_seconds: float | None = None

    def __init__(self, *, emitter: SignalEmitter, portfolio: Portfolio, settings: Settings) -> None:
        self.strategy_id = emitter.strategy_id
        self._emitter = emitter
        self._quantity = settings.quantity
        self._bought: set[str] = set()

    async def on_tick(self, tick: MarketTick) -> None:
        if tick.symbol in self._bought:
            return
        self._bought.add(tick.symbol)
        await self._emitter.place(
            symbol=tick.symbol,
            side=Side.BUY,
            quantity=self._quantity,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.IOC,
        )

    async def on_wake(self, wake: Wake) -> None:
        pass

    async def on_order_event(self, event: OrderEvent) -> None:
        pass

    def set_next_seq(self, next_seq: int) -> None:
        self._emitter.set_next_seq(next_seq)

    def snapshot(self) -> bytes:
        return json.dumps({"bought": sorted(self._bought)}).encode()

    def restore(self, data: bytes) -> None:
        self._bought = set(json.loads(data)["bought"])


class AllowsExtraSettings(BuysEachSymbolOnce):
    """Trades like its parent, but its settings model accepts any extra field."""

    class Settings(BaseModel):
        model_config = ConfigDict(extra="allow")

        quantity: Decimal


class NamesItself(BuysEachSymbolOnce):
    """Trades like its parent, but sets its own id instead of the emitter's."""

    def __init__(
        self, *, emitter: SignalEmitter, portfolio: Portfolio, settings: BuysEachSymbolOnce.Settings
    ) -> None:
        super().__init__(emitter=emitter, portfolio=portfolio, settings=settings)
        self.strategy_id = "momentum"


class NeverNamesItself(BuysEachSymbolOnce):
    """Trades like its parent, but its constructor never sets ``strategy_id``."""

    def __init__(
        self, *, emitter: SignalEmitter, portfolio: Portfolio, settings: BuysEachSymbolOnce.Settings
    ) -> None:
        self._emitter = emitter
        self._quantity = settings.quantity
        self._bought: set[str] = set()


class RaisesWhileBuilding(BuysEachSymbolOnce):
    """Takes the right arguments, but its constructor raises."""

    def __init__(
        self, *, emitter: SignalEmitter, portfolio: Portfolio, settings: BuysEachSymbolOnce.Settings
    ) -> None:
        raise ValueError("quantity is too small for this strategy")


class TakesNoPortfolio:
    """The old reference limit shape: a constructor with no ``portfolio``."""

    class Settings(BaseModel):
        pass

    def __init__(self, *, emitter: SignalEmitter, settings: Settings) -> None:
        self.strategy_id = emitter.strategy_id


class NeedsAnExtraArgument:
    """A constructor that wants one more argument than the engine passes."""

    class Settings(BaseModel):
        pass

    def __init__(
        self, *, emitter: SignalEmitter, portfolio: Portfolio, settings: Settings, clock: object
    ) -> None:
        self.strategy_id = emitter.strategy_id


class LacksTwoMethods:
    """Builds fine, but has no ``on_tick`` and no ``restore``."""

    class Settings(BaseModel):
        pass

    def __init__(self, *, emitter: SignalEmitter, portfolio: Portfolio, settings: Settings) -> None:
        self.strategy_id = emitter.strategy_id

    async def on_wake(self, wake: Wake) -> None:
        pass

    async def on_order_event(self, event: OrderEvent) -> None:
        pass

    def set_next_seq(self, next_seq: int) -> None:
        pass

    def snapshot(self) -> bytes:
        return b""


class LacksOnWake:
    """Has every Strategy member except ``on_wake``."""

    class Settings(BaseModel):
        pass

    wake_interval_seconds = None

    def __init__(self, *, emitter: SignalEmitter, portfolio: Portfolio, settings: Settings) -> None:
        self.strategy_id = emitter.strategy_id

    async def on_tick(self, tick: MarketTick) -> None:
        pass

    async def on_order_event(self, event: OrderEvent) -> None:
        pass

    def set_next_seq(self, next_seq: int) -> None:
        pass

    def snapshot(self) -> bytes:
        return b""

    def restore(self, data: bytes) -> None:
        pass


class LacksWakeInterval:
    """Has every Strategy method, but never declares ``wake_interval_seconds``."""

    class Settings(BaseModel):
        pass

    def __init__(self, *, emitter: SignalEmitter, portfolio: Portfolio, settings: Settings) -> None:
        self.strategy_id = emitter.strategy_id

    async def on_tick(self, tick: MarketTick) -> None:
        pass

    async def on_wake(self, wake: Wake) -> None:
        pass

    async def on_order_event(self, event: OrderEvent) -> None:
        pass

    def set_next_seq(self, next_seq: int) -> None:
        pass

    def snapshot(self) -> bytes:
        return b""

    def restore(self, data: bytes) -> None:
        pass


class HasNoSettings:
    """A class that forgot to declare its settings model."""


class SettingsIsNotAModel:
    """A class whose ``Settings`` is a plain class, not a Pydantic model."""

    class Settings:
        quantity = Decimal("1")
