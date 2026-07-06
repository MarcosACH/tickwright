"""Property suites for the ``StrategyHost`` recovery guarantees (issue #17).

Two structural safety nets, exercised with Hypothesis against real components:

* **Seq safety** (ADR-0016): after a restart with a *deliberately stale*
  snapshot, no emitted ``signal_id`` ever collides with a seq the saga store
  already recorded — places and cancels both consume seqs (ADR-0026), and the
  high-water recovery reads them, never the snapshot.
* **Tick-gate soundness** (ADR-0025): whatever interleaving of duplicated,
  reordered, and stale ticks the bus redelivers, ``on_tick`` observes a
  strictly increasing per-symbol ``(ts_event, trade_id)`` sequence and never a
  stale-beyond-threshold tick.
"""

import asyncio
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    AggressorSide,
    MarketTick,
    Order,
    OrderEvent,
    OrderType,
    PlaceSignal,
    Side,
    Signal,
    TimeInForce,
    derive_cloid,
)
from tickwright.engine.strategy_host import StrategyHost


class EveryTickStrategy:
    """A third-party strategy that places on every tick — the densest possible
    consumer of seqs, so any reuse after restart is caught immediately."""

    def __init__(self, strategy_id: str, bus: InMemoryBus) -> None:
        self.strategy_id = strategy_id
        self._bus = bus
        self._next_seq = 1
        self.ticks_seen = 0

    async def on_tick(self, tick: MarketTick) -> None:
        self.ticks_seen += 1
        seq = self._next_seq
        self._next_seq += 1
        await self._bus.publish(
            PlaceSignal(
                ts_event=tick.ts_event,
                ts_init=tick.ts_event,
                strategy_id=self.strategy_id,
                symbol=tick.symbol,
                seq=seq,
                side=Side.BUY,
                quantity=Decimal("1"),
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.IOC,
            )
        )

    async def on_order_event(self, event: OrderEvent) -> None:
        pass

    def set_next_seq(self, next_seq: int) -> None:
        self._next_seq = next_seq

    def snapshot(self) -> bytes:
        return b"stale-state-that-knows-nothing-of-consumed-seqs"

    def restore(self, data: bytes) -> None:
        pass  # Deliberately stale: restoring changes nothing about the seq.


def _consumed_order(signal_id: str, *, cancel_signal_id: str | None = None) -> Order:
    strategy_id, symbol, _ = signal_id.split(":")
    order = Order(
        cloid=derive_cloid(signal_id),
        strategy_id=strategy_id,
        signal_id=signal_id,
        symbol=symbol,
        side=Side.BUY,
        quantity=Decimal("1"),
        order_type=OrderType.MARKET,
    )
    if cancel_signal_id is not None:
        assert order.request_cancel(signal_id=cancel_signal_id, ts_ns=1)
    return order


def _tick(symbol: str, ts: int, trade_id: str) -> MarketTick:
    return MarketTick(
        ts_event=ts,
        ts_init=ts,
        symbol=symbol,
        price=Decimal("100"),
        size=Decimal("1"),
        aggressor_side=AggressorSide.BUY,
        trade_id=trade_id,
        seq=0,
    )


@given(
    place_seqs=st.sets(st.integers(min_value=1, max_value=50), min_size=1, max_size=8),
    cancel_seqs=st.sets(st.integers(min_value=1, max_value=50), max_size=4),
    ticks_after_restart=st.integers(min_value=1, max_value=5),
)
def test_stale_snapshot_never_reuses_a_consumed_signal_id(
    place_seqs: set[int], cancel_seqs: set[int], ticks_after_restart: int
) -> None:
    # First life: the saga store recorded these consumed seqs — places, and
    # cancels attached to some order (cancels consume seqs too, ADR-0026).
    cancel_seqs = cancel_seqs - place_seqs
    store = SQLiteStore(":memory:")
    consumed = {f"alpha:BTC:{seq}" for seq in place_seqs}
    place_list = sorted(place_seqs)
    for i, seq in enumerate(place_list):
        cancel_seq = sorted(cancel_seqs)[i] if i < len(cancel_seqs) else None
        cancel_id = f"alpha:BTC:{cancel_seq}" if cancel_seq is not None else None
        if cancel_id is not None:
            consumed.add(cancel_id)
        store.checkpoint(_consumed_order(f"alpha:BTC:{seq}", cancel_signal_id=cancel_id), ts_ns=1)
    # The snapshot the restart restores is deliberately stale: it knows
    # nothing of the consumed seqs.
    store.save_strategy_snapshot("alpha", b"stale", ts_ns=1)

    # Second life over the surviving store.
    bus = InMemoryBus()
    host = StrategyHost(bus=bus, clock=ManualClock(), store=store)
    strategy = EveryTickStrategy("alpha", bus)
    host.register(strategy, symbols={"BTC"})
    host.start()

    emitted: list[str] = []

    async def record(signal: Signal) -> None:
        emitted.append(signal.signal_id)

    bus.subscribe(Signal, record)
    for i in range(ticks_after_restart):
        asyncio.run(bus.publish(_tick("BTC", ts=1_000 + i, trade_id=f"t{i}")))

    assert len(emitted) == ticks_after_restart
    assert not set(emitted) & consumed, f"reused signal ids: {set(emitted) & consumed}"


class TickRecorder:
    """A third-party strategy that only records what gets through the gate."""

    def __init__(self, strategy_id: str) -> None:
        self.strategy_id = strategy_id
        self.ticks: list[MarketTick] = []

    async def on_tick(self, tick: MarketTick) -> None:
        self.ticks.append(tick)

    async def on_order_event(self, event: OrderEvent) -> None:
        pass

    def set_next_seq(self, next_seq: int) -> None:
        pass

    def snapshot(self) -> bytes:
        return b""

    def restore(self, data: bytes) -> None:
        pass


_NOW_NS = 10_000  # The fixed "now" the staleness gate measures against.


@given(
    btc_times=st.sets(st.integers(min_value=8_000, max_value=10_000), min_size=1, max_size=6),
    eth_times=st.sets(st.integers(min_value=8_000, max_value=10_000), min_size=1, max_size=6),
    threshold=st.integers(min_value=100, max_value=1_000),
    injections=st.lists(
        st.tuples(st.integers(min_value=0, max_value=63), st.integers(min_value=0, max_value=63)),
        max_size=8,
    ),
)
def test_on_tick_sees_only_fresh_strictly_increasing_per_symbol_ticks(
    btc_times: set[int],
    eth_times: set[int],
    threshold: int,
    injections: list[tuple[int, int]],
) -> None:
    honest = sorted(
        [_tick("BTC", ts, f"b{ts}") for ts in btc_times]
        + [_tick("ETH", ts, f"e{ts}") for ts in eth_times],
        key=lambda t: t.ts_event,
    )
    # Redeliveries: after honest position `pos`, replay an earlier-or-equal
    # tick — the duplicate / reorder an at-least-once bus can produce under
    # per-symbol ordering (ADR-0003).
    replay_after: dict[int, list[MarketTick]] = {}
    for after_raw, src_raw in injections:
        pos = after_raw % len(honest)
        src = src_raw % len(honest)
        if src > pos:
            pos, src = src, pos
        replay_after.setdefault(pos, []).append(honest[src])

    bus = InMemoryBus()
    host = StrategyHost(
        bus=bus,
        clock=ManualClock(start_ns=_NOW_NS),
        store=SQLiteStore(":memory:"),
        tick_staleness_ns=threshold,
    )
    recorder = TickRecorder("recorder")
    host.register(recorder, symbols={"BTC", "ETH"})
    host.start()

    for pos, tick in enumerate(honest):
        asyncio.run(bus.publish(tick))
        for replayed in replay_after.get(pos, []):
            asyncio.run(bus.publish(replayed))

    for symbol, times in (("BTC", btc_times), ("ETH", eth_times)):
        delivered = [t.ts_event for t in recorder.ticks if t.symbol == symbol]
        # Exactly the fresh honest ticks, in order: every duplicate, reorder,
        # and stale-beyond-threshold tick was dropped, nothing else was.
        assert delivered == [ts for ts in sorted(times) if _NOW_NS - ts <= threshold]
