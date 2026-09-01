"""``StrategyHost`` (issue #17): the engine-side strategy runtime — registry,
per-strategy routing, the monotonic tick gate, containment, and snapshot/seq
recovery (ADR-0016/0018/0024/0025).

The recording strategy here is a third-party ``Strategy`` implementation, the
seam this host exists to serve — not a mock of an engine class.
"""

import asyncio
from decimal import Decimal

import pytest
import structlog.testing

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    UNATTRIBUTED,
    AggressorSide,
    InvariantViolation,
    MarketTick,
    Order,
    OrderEvent,
    OrderPlaced,
    OrderType,
    Side,
    SignalId,
    derive_cloid,
)
from tickwright.engine.strategy_host import StrategyHost


class RecordingStrategy:
    """A minimal third-party strategy that records what the host delivers."""

    def __init__(self, strategy_id: str) -> None:
        self.strategy_id = strategy_id
        self.ticks: list[MarketTick] = []
        self.order_events: list[OrderEvent] = []
        self.state = b""
        self.next_seq = 1

    def set_next_seq(self, next_seq: int) -> None:
        self.next_seq = next_seq

    async def on_tick(self, tick: MarketTick) -> None:
        self.ticks.append(tick)

    async def on_order_event(self, event: OrderEvent) -> None:
        self.order_events.append(event)

    def snapshot(self) -> bytes:
        return self.state

    def restore(self, data: bytes) -> None:
        self.state = data


def _tick(symbol: str, *, ts: int = 1_000, trade_id: str = "a", seq: int = 1) -> MarketTick:
    return MarketTick(
        ts_event=ts,
        ts_init=ts,
        symbol=symbol,
        price=Decimal("100"),
        size=Decimal("1"),
        aggressor_side=AggressorSide.BUY,
        trade_id=trade_id,
        seq=seq,
    )


def test_strategy_receives_only_ticks_for_its_declared_symbols() -> None:
    bus = InMemoryBus()
    host = StrategyHost(bus=bus, clock=ManualClock(), store=SQLiteStore(":memory:"))
    strategy = RecordingStrategy("btc-strat")
    host.register(strategy, symbols={"BTC"})
    host.start()

    asyncio.run(bus.publish(_tick("BTC")))
    asyncio.run(bus.publish(_tick("ETH")))

    assert [tick.symbol for tick in strategy.ticks] == ["BTC"]


def _order_placed(strategy_id: str, *, symbol: str = "BTC") -> OrderPlaced:
    signal_id = f"{strategy_id}:{symbol}:1"
    return OrderPlaced(
        ts_event=1_000,
        ts_init=1_000,
        cloid=f"0x{strategy_id}",
        strategy_id=strategy_id,
        signal_id=signal_id,
        symbol=symbol,
    )


def test_strategy_receives_only_its_own_order_events() -> None:
    bus = InMemoryBus()
    host = StrategyHost(bus=bus, clock=ManualClock(), store=SQLiteStore(":memory:"))
    alpha = RecordingStrategy("alpha")
    beta = RecordingStrategy("beta")
    host.register(alpha, symbols={"BTC"})
    host.register(beta, symbols={"ETH"})
    host.start()

    asyncio.run(bus.publish(_order_placed("alpha")))
    asyncio.run(bus.publish(_order_placed("beta")))

    assert [event.strategy_id for event in alpha.order_events] == ["alpha"]
    assert [event.strategy_id for event in beta.order_events] == ["beta"]


def test_duplicate_and_out_of_order_ticks_never_reach_on_tick() -> None:
    bus = InMemoryBus()
    host = StrategyHost(bus=bus, clock=ManualClock(), store=SQLiteStore(":memory:"))
    strategy = RecordingStrategy("gated")
    host.register(strategy, symbols={"BTC"})
    host.start()

    asyncio.run(bus.publish(_tick("BTC", ts=1_000, trade_id="a")))
    asyncio.run(bus.publish(_tick("BTC", ts=1_000, trade_id="a")))  # duplicate
    asyncio.run(bus.publish(_tick("BTC", ts=900, trade_id="z")))  # out of order
    asyncio.run(bus.publish(_tick("BTC", ts=2_000, trade_id="b")))

    assert [(t.ts_event, t.trade_id) for t in strategy.ticks] == [(1_000, "a"), (2_000, "b")]


def test_tick_gate_is_independent_per_symbol() -> None:
    bus = InMemoryBus()
    host = StrategyHost(bus=bus, clock=ManualClock(), store=SQLiteStore(":memory:"))
    strategy = RecordingStrategy("multi")
    host.register(strategy, symbols={"BTC", "ETH"})
    host.start()

    asyncio.run(bus.publish(_tick("BTC", ts=2_000, trade_id="a")))
    # ETH is earlier than BTC's high-water; its own gate has seen nothing yet.
    asyncio.run(bus.publish(_tick("ETH", ts=500, trade_id="b")))

    assert [tick.symbol for tick in strategy.ticks] == ["BTC", "ETH"]


def test_same_ts_event_ticks_are_gated_by_seq_not_trade_id() -> None:
    bus = InMemoryBus()
    host = StrategyHost(bus=bus, clock=ManualClock(), store=SQLiteStore(":memory:"))
    strategy = RecordingStrategy("same-ns")
    host.register(strategy, symbols={"BTC"})
    host.start()

    # Two *distinct* trades at the identical ts_event, in source (seq) order.
    # Their trade_ids straddle a digit-width boundary, so a lexicographic
    # (ts_event, trade_id) gate would see (1_000, "10") <= (1_000, "9") and drop
    # the second, real tick. The gate keys on seq, so both get through.
    asyncio.run(bus.publish(_tick("BTC", ts=1_000, trade_id="9", seq=1)))
    asyncio.run(bus.publish(_tick("BTC", ts=1_000, trade_id="10", seq=2)))
    # An exact redelivery (same ts_event and seq) is still dropped.
    asyncio.run(bus.publish(_tick("BTC", ts=1_000, trade_id="10", seq=2)))

    assert [(t.trade_id, t.seq) for t in strategy.ticks] == [("9", 1), ("10", 2)]


def test_stale_beyond_threshold_tick_is_dropped() -> None:
    bus = InMemoryBus()
    clock = ManualClock(start_ns=10_000)
    host = StrategyHost(
        bus=bus, clock=clock, store=SQLiteStore(":memory:"), tick_staleness_ns=1_000
    )
    strategy = RecordingStrategy("live")
    host.register(strategy, symbols={"BTC"})
    host.start()

    # 10_000 - 8_000 > 1_000: a pre-crash backlog tick — never trade on it.
    asyncio.run(bus.publish(_tick("BTC", ts=8_000, trade_id="a")))
    # 10_000 - 9_500 <= 1_000: fresh enough.
    asyncio.run(bus.publish(_tick("BTC", ts=9_500, trade_id="b")))

    assert [tick.trade_id for tick in strategy.ticks] == ["b"]


def test_staleness_gate_is_off_by_default() -> None:
    bus = InMemoryBus()
    host = StrategyHost(bus=bus, clock=ManualClock(start_ns=10_000), store=SQLiteStore(":memory:"))
    strategy = RecordingStrategy("replay")
    host.register(strategy, symbols={"BTC"})
    host.start()

    asyncio.run(bus.publish(_tick("BTC", ts=1_000, trade_id="a")))

    assert [tick.trade_id for tick in strategy.ticks] == ["a"]


class RaisingStrategy(RecordingStrategy):
    """A third-party strategy whose handlers raise — the containment target."""

    def __init__(self, strategy_id: str, error: Exception) -> None:
        super().__init__(strategy_id)
        self._error = error

    async def on_tick(self, tick: MarketTick) -> None:
        raise self._error

    async def on_order_event(self, event: OrderEvent) -> None:
        raise self._error


def test_raising_strategy_emits_strategy_error_and_others_keep_receiving() -> None:
    bus = InMemoryBus()
    host = StrategyHost(bus=bus, clock=ManualClock(), store=SQLiteStore(":memory:"))
    bad = RaisingStrategy("bad", KeyError("third-party bug"))
    good = RecordingStrategy("good")
    host.register(bad, symbols={"BTC"})
    host.register(good, symbols={"ETH"})
    host.start()

    # A tick each rather than one shared: ADR-0034's disjointness rule means no
    # two strategies can declare the same symbol, so "the others keep
    # receiving" is asserted across the raise rather than within one dispatch.
    tick = _tick("BTC", ts=1_000, trade_id="a")
    with structlog.testing.capture_logs() as logs:
        asyncio.run(bus.publish(tick))
        asyncio.run(bus.publish(_tick("ETH", ts=1_000, trade_id="a")))

    assert [t.trade_id for t in good.ticks] == ["a"]
    errors = [log for log in logs if log["event"] == "strategy.error"]
    assert len(errors) == 1
    assert errors[0]["strategy_id"] == "bad"
    assert errors[0]["event_id"] == tick.event_id


def test_raising_on_order_event_is_contained_too() -> None:
    bus = InMemoryBus()
    host = StrategyHost(bus=bus, clock=ManualClock(), store=SQLiteStore(":memory:"))
    bad = RaisingStrategy("bad", ValueError("boom"))
    host.register(bad, symbols={"BTC"})
    host.start()

    event = _order_placed("bad")
    with structlog.testing.capture_logs() as logs:
        asyncio.run(bus.publish(event))

    errors = [log for log in logs if log["event"] == "strategy.error"]
    assert len(errors) == 1
    assert errors[0]["event_id"] == event.event_id


def test_invariant_violation_pierces_containment() -> None:
    bus = InMemoryBus()
    host = StrategyHost(bus=bus, clock=ManualClock(), store=SQLiteStore(":memory:"))
    host.register(
        RaisingStrategy("bad", InvariantViolation("broken engine assumption")), symbols={"BTC"}
    )
    host.start()

    with pytest.raises(InvariantViolation):
        asyncio.run(bus.publish(_tick("BTC")))


def test_stop_persists_a_final_snapshot_per_strategy() -> None:
    store = SQLiteStore(":memory:")
    host = StrategyHost(bus=InMemoryBus(), clock=ManualClock(), store=store)
    alpha = RecordingStrategy("alpha")
    beta = RecordingStrategy("beta")
    alpha.state = b"alpha-final"
    beta.state = b"beta-final"
    host.register(alpha, symbols={"BTC"})
    host.register(beta, symbols={"ETH"})
    host.start()

    host.stop()

    assert store.load_strategy_snapshot("alpha") == b"alpha-final"
    assert store.load_strategy_snapshot("beta") == b"beta-final"


def test_start_restores_the_persisted_snapshot() -> None:
    store = SQLiteStore(":memory:")
    store.save_strategy_snapshot("alpha", b"prior-life", ts_ns=1_000)
    host = StrategyHost(bus=InMemoryBus(), clock=ManualClock(), store=store)
    alpha = RecordingStrategy("alpha")
    host.register(alpha, symbols={"BTC"})

    host.start()

    assert alpha.state == b"prior-life"


def test_start_without_a_snapshot_leaves_the_strategy_fresh() -> None:
    host = StrategyHost(bus=InMemoryBus(), clock=ManualClock(), store=SQLiteStore(":memory:"))
    alpha = RecordingStrategy("alpha")
    alpha.state = b"untouched"
    host.register(alpha, symbols={"BTC"})

    host.start()

    assert alpha.state == b"untouched"


class IncompatibleRestoreStrategy(RecordingStrategy):
    """A strategy whose code changed shape between runs: restore() rejects."""

    def restore(self, data: bytes) -> None:
        raise ValueError("unknown snapshot version")


def test_incompatible_snapshot_starts_fresh_with_a_named_event() -> None:
    store = SQLiteStore(":memory:")
    store.save_strategy_snapshot("alpha", b"old-shape", ts_ns=1_000)
    bus = InMemoryBus()
    host = StrategyHost(bus=bus, clock=ManualClock(), store=store)
    alpha = IncompatibleRestoreStrategy("alpha")
    host.register(alpha, symbols={"BTC"})

    with structlog.testing.capture_logs() as logs:
        host.start()

    incompatible = [log for log in logs if log["event"] == "strategy.snapshot_incompatible"]
    assert len(incompatible) == 1
    assert incompatible[0]["strategy_id"] == "alpha"
    # The engine is unaffected: the strategy runs fresh and still gets ticks.
    asyncio.run(bus.publish(_tick("BTC")))
    assert len(alpha.ticks) == 1


class InvariantViolatingRestoreStrategy(RecordingStrategy):
    """A strategy whose restore() signals a broken *engine* assumption."""

    def restore(self, data: bytes) -> None:
        raise InvariantViolation("broken engine assumption in restore")


def test_invariant_violation_in_restore_pierces_and_faults_start() -> None:
    store = SQLiteStore(":memory:")
    store.save_strategy_snapshot("alpha", b"any", ts_ns=1_000)
    host = StrategyHost(bus=InMemoryBus(), clock=ManualClock(), store=store)
    host.register(InvariantViolatingRestoreStrategy("alpha"), symbols={"BTC"})

    # Unlike an incompatible snapshot (start-fresh), an InvariantViolation is a
    # broken engine assumption: it pierces the restore net and faults start().
    with pytest.raises(InvariantViolation, match="broken engine assumption"):
        host.start()


def _checkpointed_order(
    store: SQLiteStore, signal_id: str, *, cancel_signal_id: str | None = None
) -> None:
    parsed = SignalId.parse(signal_id)
    order = Order(
        cloid=derive_cloid(signal_id),
        strategy_id=parsed.strategy_id,
        signal_id=signal_id,
        symbol=parsed.symbol,
        side=Side.BUY,
        quantity=Decimal("1"),
        order_type=OrderType.LIMIT,
    )
    if cancel_signal_id is not None:
        assert order.request_cancel(signal_id=cancel_signal_id, ts_ns=1_000)
    store.checkpoint(order, ts_ns=1_000)


def test_start_recovers_next_seq_from_the_saga_high_water() -> None:
    store = SQLiteStore(":memory:")
    # alpha consumed seq 1 (BTC) and seq 3 (ETH); a cancel consumed seq 5.
    _checkpointed_order(store, "alpha:BTC:1")
    _checkpointed_order(store, "alpha:ETH:3", cancel_signal_id="alpha:ETH:5")
    # Another strategy's records never leak into alpha's high-water.
    _checkpointed_order(store, "other:SOL:9")

    host = StrategyHost(bus=InMemoryBus(), clock=ManualClock(), store=store)
    alpha = RecordingStrategy("alpha")
    other = RecordingStrategy("other")
    host.register(alpha, symbols={"BTC", "ETH"})
    host.register(other, symbols={"SOL"})
    host.start()

    assert alpha.next_seq == 6
    assert other.next_seq == 10


def test_start_with_no_saga_records_starts_seq_at_one() -> None:
    host = StrategyHost(bus=InMemoryBus(), clock=ManualClock(), store=SQLiteStore(":memory:"))
    alpha = RecordingStrategy("alpha")
    host.register(alpha, symbols={"BTC"})
    host.start()

    assert alpha.next_seq == 1


def test_duplicate_strategy_id_registration_fails_fast() -> None:
    host = StrategyHost(bus=InMemoryBus(), clock=ManualClock(), store=SQLiteStore(":memory:"))
    host.register(RecordingStrategy("dup"), symbols={"BTC"})

    with pytest.raises(InvariantViolation, match="dup"):
        host.register(RecordingStrategy("dup"), symbols={"ETH"})


def test_registering_the_reserved_unattributed_id_fails_fast() -> None:
    """The last gate before an id keys a ledger row (ADR-0043 §2).

    ``StrategyConfig`` refuses the literal earlier, but a strategy registered
    without going through a config — a test, an embedding host — never meets
    that validator, and this is where the id starts partitioning fills.
    """
    host = StrategyHost(bus=InMemoryBus(), clock=ManualClock(), store=SQLiteStore(":memory:"))

    with pytest.raises(InvariantViolation, match=UNATTRIBUTED):
        host.register(RecordingStrategy(UNATTRIBUTED), symbols={"BTC"})


def test_a_second_strategy_may_not_claim_an_owned_symbol() -> None:
    """ADR-0034's disjointness rule, enforced rather than assumed.

    On a ``NET`` venue two same-symbol strategies are netted into one real
    position: one's close silently changes the other's exposure and liquidation
    is account-wide, so engine-side per-strategy books would stay arithmetically
    consistent while describing an isolation the venue does not provide. The
    error names both strategies and the symbol because the remedy is a separate
    account, and a reader cannot pick which strategy to move without them.
    """
    host = StrategyHost(bus=InMemoryBus(), clock=ManualClock(), store=SQLiteStore(":memory:"))
    host.register(RecordingStrategy("alpha"), symbols={"BTC", "ETH"})

    with pytest.raises(InvariantViolation) as raised:
        host.register(RecordingStrategy("beta"), symbols={"ETH", "SOL"})

    message = str(raised.value)
    assert "alpha" in message and "beta" in message and "ETH" in message
    assert "SOL" not in message


def test_strategies_on_disjoint_symbols_register_and_route_independently() -> None:
    """The disjointness gate refuses overlap, not multi-strategy hosting.

    The rule is narrow on purpose — one symbol, one owner — so a host running
    several strategies over a partitioned universe is the *supported* shape, not
    a tolerated one. Asserted through routing rather than through the absence of
    a raise: the gate maintains its own symbol-to-owner index beside the
    per-strategy sets the subscriptions read, and an index that drifted from
    them would still register these three without complaint.
    """
    bus = InMemoryBus()
    host = StrategyHost(bus=bus, clock=ManualClock(), store=SQLiteStore(":memory:"))
    alpha = RecordingStrategy("alpha")
    beta = RecordingStrategy("beta")
    gamma = RecordingStrategy("gamma")
    host.register(alpha, symbols={"BTC", "ETH"})
    host.register(beta, symbols={"SOL"})
    host.register(gamma, symbols={"DOGE"})
    host.start()

    for symbol in ("BTC", "ETH", "SOL", "DOGE"):
        asyncio.run(bus.publish(_tick(symbol)))

    assert sorted(tick.symbol for tick in alpha.ticks) == ["BTC", "ETH"]
    assert [tick.symbol for tick in beta.ticks] == ["SOL"]
    assert [tick.symbol for tick in gamma.ticks] == ["DOGE"]
