"""Five throwaway strategies that read the ``Portfolio`` seam, for verification only.

They are not shipped ``kind``s. ``docs/extending.md`` keeps a third strategy
out of tree, so these satisfy the ``Strategy`` Protocol by shape and are
registered by hand in ``run_strategy.py``. Each one logs what it read as a
``verify.*`` event on the same stderr stream the engine writes, so the evidence
shows the strategy's view beside the engine's.

Every one is single-flight: it never has two orders open at once. Its phase goes
in the snapshot, and the engine saves that after every callback that changes it
(ADR-0016, #348), so a restart resumes the phase after a graceful stop and after
a crash alike (see features/crash-recovery.md).
"""

from __future__ import annotations

import dataclasses
import json
import time
from decimal import Decimal

import structlog

from tickwright.domain import (
    MarketTick,
    OrderEvent,
    OrderFilled,
    OrderType,
    Portfolio,
    Side,
    SignalEmitter,
    TimeInForce,
)

log = structlog.get_logger()


def _plain(value: object) -> object:
    """Decimals as strings, dataclasses as dicts, so a log line stays JSON."""
    if isinstance(value, Decimal):
        return str(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, tuple | list):
        return [_plain(v) for v in value]
    return value


@dataclasses.dataclass(frozen=True, kw_only=True)
class Wiring:
    """What every verification strategy is built from, bundled so the
    constructors stay typed without repeating the same keyword arguments each."""

    emitter: SignalEmitter
    portfolio: Portfolio
    symbol: str
    quantity: Decimal
    # On Hyperliquid, a market order sent before the first mark faults the
    # engine (features/safe-exit.md, Gotchas). A wait lets a mark arrive first.
    wait_seconds: int = 0


class _Base:
    """The shared spine: emitter, portfolio, phase snapshot, read logging."""

    version = 1

    def __init__(self, wiring: Wiring) -> None:
        self.strategy_id = wiring.emitter.strategy_id
        self._emitter = wiring.emitter
        self._portfolio = wiring.portfolio
        self._symbol = wiring.symbol
        self._quantity = wiring.quantity
        self._wait_seconds = wiring.wait_seconds
        self._first_seen: float | None = None
        self._phase = "idle"
        self._ticks_seen = 0

    def _waited(self) -> bool:
        """Whether ``wait_seconds`` of wall time have passed since the first trade.

        Wall time, not trade time: the Hyperliquid feed's first batch can carry
        trades older than the wait, so trade time can pass it at once.
        """
        if self._first_seen is None:
            self._first_seen = time.monotonic()
        return time.monotonic() - self._first_seen >= self._wait_seconds

    def _read(self, event: str, **extra: object) -> None:
        position = self._portfolio.position(self._symbol)
        log.info(
            event,
            strategy_id=self.strategy_id,
            phase=self._phase,
            position=_plain(position),
            account=_plain(self._portfolio.account()),
            **{k: _plain(v) for k, v in extra.items()},
        )

    async def _place(self, side: Side) -> None:
        await self._emitter.place(
            symbol=self._symbol,
            side=side,
            quantity=self._quantity,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.IOC,
        )

    async def on_order_event(self, event: OrderEvent) -> None:
        if isinstance(event, OrderFilled):
            self._read("verify.fill", cloid=event.cloid, signal_id=event.signal_id)
            await self._after_fill()

    async def _after_fill(self) -> None:
        pass

    def set_next_seq(self, next_seq: int) -> None:
        self._emitter.set_next_seq(next_seq)

    def snapshot(self) -> bytes:
        return json.dumps(
            {"version": self.version, "phase": self._phase, "ticks_seen": self._ticks_seen}
        ).encode()

    def restore(self, data: bytes) -> None:
        state = json.loads(data)
        if state.get("version") != self.version:
            raise ValueError(f"unknown snapshot version: {state.get('version')!r}")
        self._phase = state["phase"]
        self._ticks_seen = int(state["ticks_seen"])


class RoundTripThenFlat(_Base):
    """Buy on the first tick, hold ``hold_ticks`` ticks, sell. Proves the position
    returns to flat and that realized PnL and fees match the two fills."""

    def __init__(self, wiring: Wiring, *, hold_ticks: int) -> None:
        super().__init__(wiring)
        self._hold_ticks = hold_ticks
        self._held = 0

    async def on_tick(self, tick: MarketTick) -> None:
        self._ticks_seen += 1
        if self._phase == "idle" and self._waited():
            self._phase = "buying"
            await self._place(Side.BUY)
        elif self._phase == "holding":
            self._held += 1
            self._read("verify.tick", mark=tick.price)
            if self._held >= self._hold_ticks:
                self._phase = "selling"
                await self._place(Side.SELL)

    async def _after_fill(self) -> None:
        if self._phase == "buying":
            self._phase = "holding"
        elif self._phase == "selling":
            self._phase = "flat"
            self._read("verify.flat")


class TakeProfit(_Base):
    """Buy on the first tick, then sell once ``position().unrealized_pnl`` reaches
    ``take_profit``. Proves the mark-driven read the strategy acts on."""

    def __init__(self, wiring: Wiring, *, take_profit: Decimal) -> None:
        super().__init__(wiring)
        self._take_profit = take_profit

    async def on_tick(self, tick: MarketTick) -> None:
        self._ticks_seen += 1
        if self._phase == "idle":
            self._phase = "buying"
            await self._place(Side.BUY)
        elif self._phase == "holding":
            position = self._portfolio.position(self._symbol)
            upnl = position.unrealized_pnl if position is not None else None
            self._read("verify.tick", mark=tick.price, upnl=upnl, target=self._take_profit)
            if upnl is not None and upnl >= self._take_profit:
                self._phase = "selling"
                await self._place(Side.SELL)

    async def _after_fill(self) -> None:
        if self._phase == "buying":
            self._phase = "holding"
        elif self._phase == "selling":
            self._phase = "done"
            self._read("verify.done")


class MarginWatch(_Base):
    """Buy once, then only read. Logs equity, margin used, free margin and the
    liquidation price on every tick. Proves the margin math at the configured
    leverage, and that a negative free margin is reported and never acted on."""

    async def on_tick(self, tick: MarketTick) -> None:
        self._ticks_seen += 1
        if self._phase == "idle":
            self._phase = "buying"
            await self._place(Side.BUY)
        elif self._phase == "holding":
            self._read("verify.tick", mark=tick.price)

    async def _after_fill(self) -> None:
        if self._phase == "buying":
            self._phase = "holding"


class RestThenHold(_Base):
    """Buy at market, then rest a buy at ``rest_below`` times the last trade price.

    It leaves what a safe exit has to clean up: a position and a resting order
    that will not fill. See features/safe-exit.md.
    """

    def __init__(self, wiring: Wiring, *, rest_below: Decimal) -> None:
        super().__init__(wiring)
        self._rest_below = rest_below
        self._last_price: Decimal | None = None

    async def on_tick(self, tick: MarketTick) -> None:
        self._ticks_seen += 1
        self._last_price = tick.price
        if self._phase == "idle" and self._waited():
            self._phase = "buying"
            await self._place(Side.BUY)

    async def _after_fill(self) -> None:
        if self._phase != "buying" or self._last_price is None:
            return
        self._phase = "resting"
        price = self._last_price * self._rest_below
        await self._emitter.place(
            symbol=self._symbol,
            side=Side.BUY,
            quantity=self._quantity,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.GTC,
            price=price,
        )
        self._read("verify.rested", price=price)


class DualSymbolLeg(_Base):
    """One leg of a two-strategy book: ``side`` on ``symbol`` once, then read.
    Two of these, BTC long and ETH short, prove that each strategy sees only its
    own partition while both read the one shared account."""

    def __init__(self, wiring: Wiring, *, side: Side) -> None:
        super().__init__(wiring)
        self._side = side

    async def on_tick(self, tick: MarketTick) -> None:
        self._ticks_seen += 1
        if self._phase == "idle":
            self._phase = "buying"
            await self._place(self._side)
        elif self._phase == "holding":
            self._read("verify.tick", mark=tick.price, open=self._portfolio.open_positions())

    async def _after_fill(self) -> None:
        if self._phase == "buying":
            self._phase = "holding"
