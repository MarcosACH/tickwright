"""``SingleShotLimitStrategy`` — the resting-LIMIT + cancel reference ``Strategy``
(ADR-0016, issue #13).

Proves the seam across the second order shape and the cancel path: on its first
tick it emits exactly one LIMIT ``PlaceSignal``; an optional ``cancel_after_ticks``
makes it emit exactly one ``CancelSignal`` after that many later ticks, targeting
its own order **by the ``signal_id`` it emitted** — the strategy never derives or
sees a ``cloid`` (ADR-0006/0026). It records the ``OrderFilled`` and
``OrderCancelled`` it receives. It owns its monotonic ``seq`` so both signals are
deterministic and replayable, and knows nothing of the saga, store, or venue.
"""

import json
from decimal import Decimal

from tickwright.domain import (
    Clock,
    EventBus,
    InvariantViolation,
    MarketTick,
    OrderCancelled,
    OrderEvent,
    OrderFilled,
    OrderType,
    Side,
    TimeInForce,
)

from .emitter import SignalEmitter


class SingleShotLimitStrategy:
    """Places one resting LIMIT on the first tick; optionally cancels it later."""

    def __init__(
        self,
        *,
        strategy_id: str,
        bus: EventBus,
        clock: Clock,
        side: Side,
        quantity: Decimal,
        price: Decimal,
        time_in_force: TimeInForce = TimeInForce.GTC,
        post_only: bool = False,
        cancel_after_ticks: int | None = None,
    ) -> None:
        self.strategy_id = strategy_id
        self._emitter = SignalEmitter(strategy_id=strategy_id, bus=bus, clock=clock)
        self._side = side
        self._quantity = quantity
        self._price = price
        self._time_in_force = time_in_force
        self._post_only = post_only
        self._cancel_after_ticks = cancel_after_ticks
        # The emitter owns the seq counter (engine-set via set_next_seq, ADR-0016);
        # everything below is the snapshot content this strategy owns.
        self._placed_signal_id: str | None = None
        self._ticks_since_place = 0
        self._cancelled = False
        self.fills: list[OrderFilled] = []
        self.cancellations: list[OrderCancelled] = []

    async def on_tick(self, tick: MarketTick) -> None:
        if self._placed_signal_id is None:
            await self._place(tick)
            return
        await self._maybe_cancel(tick)

    async def _place(self, tick: MarketTick) -> None:
        self._placed_signal_id = await self._emitter.place(
            symbol=tick.symbol,
            side=self._side,
            quantity=self._quantity,
            order_type=OrderType.LIMIT,
            time_in_force=self._time_in_force,
            price=self._price,
            post_only=self._post_only,
        )

    async def _maybe_cancel(self, tick: MarketTick) -> None:
        if self._cancel_after_ticks is None or self._cancelled:
            return
        if self._placed_signal_id is None:
            # on_tick only routes here after a place; a missing id is a broken assumption.
            raise InvariantViolation("cancel path reached before a signal was placed")
        self._ticks_since_place += 1
        if self._ticks_since_place < self._cancel_after_ticks:
            return
        self._cancelled = True
        await self._emitter.cancel(symbol=tick.symbol, target_signal_id=self._placed_signal_id)

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
                "version": 1,
                "placed_signal_id": self._placed_signal_id,
                "ticks_since_place": self._ticks_since_place,
                "cancelled": self._cancelled,
            }
        ).encode()

    def restore(self, data: bytes) -> None:
        """Rebuild from ``snapshot()`` bytes; raises on an unknown shape, which
        the engine treats as start-fresh (ADR-0016)."""
        state = json.loads(data)
        if state.get("version") != 1:
            raise ValueError(f"unknown snapshot version: {state.get('version')!r}")
        self._placed_signal_id = state["placed_signal_id"]
        self._ticks_since_place = int(state["ticks_since_place"])
        self._cancelled = bool(state["cancelled"])
