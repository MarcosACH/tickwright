"""``SignalEmitter`` — the strategy-author's seq + publish helper (ADR-0016).

A ``Strategy`` owns its monotonic ``seq`` so its ``signal_id``s are deterministic
and replayable (ADR-0006) and never reused after a restart (the engine resumes
the counter from the store high-water via ``set_next_seq``, ADR-0016). That counter
is a correctness spine, not strategy business logic — so this helper concentrates
it, plus the clock-stamped envelope and the publish, behind typed methods.
It writes each seq to the store's seq record before the publish. So a signal a
crash leaves unhandled on Kafka still raises the high-water (#442).
A strategy *composes* an emitter (holds one as a field) rather than inheriting a
base: the ``Strategy`` seam is satisfied by shape, never by inheritance (ADR-0032).
It lives in ``domain`` so the engine's own operator jobs keep their seq the same
way. One counter rule then serves every signal source. It emits no named events.
"""

from collections.abc import Callable
from decimal import Decimal

from .enums import OrderType, Side, TimeInForce
from .events import CancelAllSignal, CancelSignal, PlaceSignal
from .protocols import Clock, EventBus


class SignalEmitter:
    """Owns a strategy's ``seq`` counter and builds/publishes its ``Signal``s."""

    def __init__(
        self,
        *,
        strategy_id: str,
        bus: EventBus,
        clock: Clock,
        record_seq: Callable[[str, int], None],
    ) -> None:
        self._strategy_id = strategy_id
        self._bus = bus
        self._clock = clock
        self._record_seq = record_seq
        self._next_seq = 1

    @property
    def strategy_id(self) -> str:
        """The id every signal from this emitter carries.

        A strategy takes its own id from here. A separate argument could name
        another strategy, and then its seq would resume from the wrong record."""
        return self._strategy_id

    def set_next_seq(self, next_seq: int) -> None:
        """Resume the ``seq`` counter from the engine-recovered high-water (ADR-0016).

        Called by the engine at startup with the store's high-water mark plus
        one — never derived from a snapshot, so a stale snapshot can never reuse a
        sent ``signal_id``."""
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
        reduce_only: bool = False,
    ) -> str:
        """Publish a ``PlaceSignal`` for the next ``seq``; return its ``signal_id``.

        The returned id is the handle the strategy references its own order by —
        e.g. to target it in a later ``cancel`` (the strategy never sees a
        ``cloid``, ADR-0026).

        A ``reduce_only`` order can only shrink the account's net position. The
        venue shrinks it to the net or rejects it (ADR-0057)."""
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
            reduce_only=reduce_only,
        )
        await self._publish(signal)
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
        await self._publish(signal)
        return signal.signal_id

    async def cancel_all(self, *, symbol: str) -> str:
        """Publish a ``CancelAllSignal`` for ``symbol``; return its ``signal_id``.

        It cancels every open order this strategy sent earlier on ``symbol``.
        Each order still ends with its own ``OrderCancelled`` or fill (ADR-0055)."""
        seq, now = self._next()
        signal = CancelAllSignal(
            ts_event=now,
            ts_init=now,
            strategy_id=self._strategy_id,
            symbol=symbol,
            seq=seq,
        )
        await self._publish(signal)
        return signal.signal_id

    async def _publish(self, signal: PlaceSignal | CancelSignal | CancelAllSignal) -> None:
        # Record before the publish. On Kafka a crash can leave the signal in the
        # topic with no saga, and the restart must not hand its seq out again (#442).
        self._record_seq(self._strategy_id, signal.seq)
        await self._bus.publish(signal)

    def _next(self) -> tuple[int, int]:
        """Allocate the next ``(seq, clock-stamped ns)`` for a signal envelope."""
        seq = self._next_seq
        self._next_seq += 1
        return seq, self._clock.timestamp_ns()
