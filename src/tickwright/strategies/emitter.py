"""``SignalEmitter`` — the strategy-author's seq + publish helper (ADR-0016).

A ``Strategy`` owns its monotonic ``seq`` so its ``signal_id``s are deterministic
and replayable (ADR-0006) and never reused after a restart (the engine resumes
the counter from the saga high-water via ``set_next_seq``, ADR-0016). That counter
is a correctness spine, not strategy business logic — so this helper concentrates
it, plus the clock-stamped envelope and the publish, behind two typed methods.
A strategy *composes* an emitter (holds one as a field) rather than inheriting a
base: the ``Strategy`` seam is satisfied by shape, never by inheritance (ADR-0032).
Depends on ``domain`` only, so it emits no named events.
"""

from decimal import Decimal

from tickwright.domain import (
    CancelSignal,
    Clock,
    EventBus,
    OrderType,
    PlaceSignal,
    Side,
    TimeInForce,
)


class SignalEmitter:
    """Owns a strategy's ``seq`` counter and builds/publishes its ``Signal``s."""

    def __init__(self, *, strategy_id: str, bus: EventBus, clock: Clock) -> None:
        self._strategy_id = strategy_id
        self._bus = bus
        self._clock = clock
        self._next_seq = 1

    def set_next_seq(self, next_seq: int) -> None:
        """Resume the ``seq`` counter from the engine-recovered high-water (ADR-0016).

        Called by the engine at startup with the saga store's high-water mark plus
        one — never derived from a snapshot, so a stale snapshot can never reuse a
        consumed ``signal_id``."""
        self._next_seq = next_seq

    async def place(
        self,
        *,
        symbol: str,
        side: Side,
        quantity: Decimal,
        order_type: OrderType,
        time_in_force: TimeInForce,
        price: Decimal | None = None,
        post_only: bool = False,
    ) -> str:
        """Publish a ``PlaceSignal`` for the next ``seq``; return its ``signal_id``.

        The returned id is the handle the strategy references its own order by —
        e.g. to target it in a later ``cancel`` (the strategy never sees a
        ``cloid``, ADR-0026)."""
        seq, now = self._next()
        signal = PlaceSignal(
            ts_event=now,
            ts_init=now,
            strategy_id=self._strategy_id,
            symbol=symbol,
            seq=seq,
            side=side,
            quantity=quantity,
            order_type=order_type,
            time_in_force=time_in_force,
            price=price,
            post_only=post_only,
        )
        await self._bus.publish(signal)
        return signal.signal_id

    async def cancel(self, *, symbol: str, target_signal_id: str) -> str:
        """Publish a ``CancelSignal`` targeting ``target_signal_id``; return its id.

        A cancel is a fresh, replayable intent carrying its own seq'd ``signal_id``
        plus the ``signal_id`` of the order to cancel (ADR-0026)."""
        seq, now = self._next()
        signal = CancelSignal(
            ts_event=now,
            ts_init=now,
            strategy_id=self._strategy_id,
            symbol=symbol,
            seq=seq,
            target_signal_id=target_signal_id,
        )
        await self._bus.publish(signal)
        return signal.signal_id

    def _next(self) -> tuple[int, int]:
        """Allocate the next ``(seq, clock-stamped ns)`` for a signal envelope."""
        seq = self._next_seq
        self._next_seq += 1
        return seq, self._clock.timestamp_ns()
