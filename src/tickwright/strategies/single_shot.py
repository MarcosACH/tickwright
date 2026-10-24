"""``SingleShotMarketStrategy`` — the minimal reference ``Strategy`` (ADR-0016).

Emits exactly one MARKET order on its first tick, then stays quiet, and records
the ``OrderFilled`` it receives together with the position that fill produced. It
owns its monotonic ``seq`` (so ``signal_id`` is deterministic and replayable,
ADR-0006) and knows nothing of the saga, the store, or the venue — the whole
point of the surrounding engine is that a strategy stays this small.

It is also the ``Portfolio`` seam's only consumer (ADR-0041 §8). The facade
arrives **already scoped** to this ``strategy_id``, constructor-injected beside
the emitter, which is what keeps ``strategies`` free of an
``engine`` import and the ``Strategy`` Protocol unchanged. Depends on ``domain``
only, plus pydantic for its ``Settings`` (ADR-0061); it emits no named events
itself.
"""

import json
from decimal import Decimal

from pydantic import BaseModel

from tickwright.domain import (
    MarketTick,
    OrderEvent,
    OrderFilled,
    OrderType,
    Portfolio,
    PositionView,
    Side,
    SignalEmitter,
    TimeInForce,
    Wake,
)


class SingleShotMarketStrategy:
    """Buys (or sells) a fixed quantity at market, once, on the first tick seen."""

    class Settings(BaseModel):
        side: Side
        quantity: Decimal

    wake_interval_seconds: float | None = None

    def __init__(self, *, emitter: SignalEmitter, portfolio: Portfolio, settings: Settings) -> None:
        # The id comes from the emitter, so the two cannot name different
        # strategies (#442).
        self.strategy_id = emitter.strategy_id
        self._emitter = emitter
        # The ``domain`` Protocol, never the ``engine`` projection behind it, and
        # never the facade class either — the seam exists for exactly that
        # dependency direction (ADR-0035, ADR-0041 §8).
        self._portfolio = portfolio
        self._side = settings.side
        self._quantity = settings.quantity
        # The emitter owns the seq counter (engine-set from the saga high-water,
        # ADR-0016); the fired flag is the snapshot content this strategy owns.
        self._fired = False
        self.fills: list[OrderFilled] = []
        # The observable record of what this strategy read, the same shape as
        # ``fills`` and written in the same handler — so a test asserts the
        # traced position through the strategy's own surface rather than
        # reaching into the projection.
        self.positions: list[PositionView | None] = []

    async def on_tick(self, tick: MarketTick) -> None:
        if self._fired:
            return  # Single shot: one order, on the first tick only.
        self._fired = True
        await self._emitter.place(
            symbol=tick.symbol,
            side=self._side,
            quantity=self._quantity,
            order_type=OrderType.MARKET,
            # A market order never rests, so IOC is its only honest tif.
            time_in_force=TimeInForce.IOC,
        )

    async def on_wake(self, wake: Wake) -> None:
        pass

    async def on_order_event(self, event: OrderEvent) -> None:
        if isinstance(event, OrderFilled):
            self.fills.append(event)
            # Coherent by construction, not by luck: the projection is the
            # fill's writer and applied it before publishing, so this read is
            # the one that fill produced. Synchronous, so a second read in this
            # handler could not straddle another fill either (ADR-0041 §7).
            self.positions.append(self._portfolio.position(event.symbol))

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
