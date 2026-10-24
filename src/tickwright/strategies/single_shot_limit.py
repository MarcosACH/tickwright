"""``SingleShotLimitStrategy`` — the resting-LIMIT + cancel reference ``Strategy``
(ADR-0016, issue #13).

Proves the seam across the second order shape and the cancel path: on its first
tick it emits exactly one LIMIT ``PlaceSignal``; an optional ``cancel_after_seconds``
makes it emit exactly one ``CancelSignal`` on the first wake-up at or past the
placing tick's ``ts_event`` plus that many seconds (ADR-0062), targeting its own
order **by the ``signal_id`` it emitted** — the strategy never derives or
sees a ``cloid`` (ADR-0006/0026). It records the ``OrderFilled`` and
``OrderCancelled`` it receives. It owns its monotonic ``seq`` so both signals are
deterministic and replayable, and knows nothing of the saga, store, or venue.
"""

import json
from decimal import Decimal

from pydantic import BaseModel, PositiveInt

from tickwright.domain import (
    InvariantViolation,
    MarketTick,
    OrderCancelled,
    OrderEvent,
    OrderFilled,
    OrderType,
    Portfolio,
    Side,
    SignalEmitter,
    TimeInForce,
    Wake,
)


class SingleShotLimitStrategy:
    """Places one resting LIMIT on the first tick; optionally cancels it later."""

    class Settings(BaseModel):
        side: Side
        quantity: Decimal
        price: Decimal
        time_in_force: TimeInForce = TimeInForce.GTC
        post_only: bool = False
        cancel_after_seconds: PositiveInt | None = None

    # Fixed, not a setting. A 1 s grid is fine enough for a whole-second deadline.
    wake_interval_seconds: float | None = 1.0

    def __init__(self, *, emitter: SignalEmitter, portfolio: Portfolio, settings: Settings) -> None:
        # It reads no position state, so it ignores the portfolio. It still
        # accepts one, because every strategy gets the same three arguments
        # (ADR-0041 §7 as amended by ADR-0061).
        del portfolio
        # The id comes from the emitter, so the two cannot name different
        # strategies (#442).
        self.strategy_id = emitter.strategy_id
        self._emitter = emitter
        self._side = settings.side
        self._quantity = settings.quantity
        self._price = settings.price
        self._time_in_force = settings.time_in_force
        self._post_only = settings.post_only
        self._cancel_after_seconds = settings.cancel_after_seconds
        # The emitter owns the seq counter (engine-set via set_next_seq, ADR-0016);
        # everything below is the snapshot content this strategy owns.
        self._placed_signal_id: str | None = None
        self._symbol: str | None = None
        self._deadline_ns: int | None = None
        self._cancelled = False
        self.fills: list[OrderFilled] = []
        self.cancellations: list[OrderCancelled] = []

    async def on_tick(self, tick: MarketTick) -> None:
        if self._placed_signal_id is None:
            await self._place(tick)

    async def _place(self, tick: MarketTick) -> None:
        # A strategy has no clock, so the deadline comes from the placing tick's
        # own time. That keeps a replay the same on every run.
        self._symbol = tick.symbol
        if self._cancel_after_seconds is not None:
            self._deadline_ns = tick.ts_event + self._cancel_after_seconds * 1_000_000_000
        self._placed_signal_id = await self._emitter.place(
            symbol=tick.symbol,
            side=self._side,
            quantity=self._quantity,
            order_type=OrderType.LIMIT,
            time_in_force=self._time_in_force,
            price=self._price,
            post_only=self._post_only,
        )

    async def on_wake(self, wake: Wake) -> None:
        if self._deadline_ns is None or self._cancelled or wake.ts_event < self._deadline_ns:
            return
        if self._placed_signal_id is None or self._symbol is None:
            # A deadline is only set by a place, so a missing id is a broken assumption.
            raise InvariantViolation("cancel path reached before a signal was placed")
        self._cancelled = True
        await self._emitter.cancel(symbol=self._symbol, target_signal_id=self._placed_signal_id)

    async def on_order_event(self, event: OrderEvent) -> None:
        if isinstance(event, OrderFilled):
            self.fills.append(event)
        elif isinstance(event, OrderCancelled):
            self.cancellations.append(event)

    def set_next_seq(self, next_seq: int) -> None:
        """Resume the seq counter from the engine-recovered high-water (ADR-0016)."""
        self._emitter.set_next_seq(next_seq)

    def snapshot(self) -> bytes:
        """State content only — the seq never travels in the snapshot."""
        return json.dumps(
            {
                "version": 2,
                "placed_signal_id": self._placed_signal_id,
                "symbol": self._symbol,
                "deadline_ns": self._deadline_ns,
                "cancelled": self._cancelled,
            }
        ).encode()

    def restore(self, data: bytes) -> None:
        """Rebuild from ``snapshot()`` bytes; raises on an unknown shape, which
        the engine treats as start-fresh (ADR-0016)."""
        state = json.loads(data)
        if state.get("version") != 2:
            raise ValueError(f"unknown snapshot version: {state.get('version')!r}")
        self._placed_signal_id = state["placed_signal_id"]
        self._symbol = state["symbol"]
        self._deadline_ns = state["deadline_ns"]
        self._cancelled = bool(state["cancelled"])
