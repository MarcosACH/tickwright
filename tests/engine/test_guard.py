"""The pre-trade guard: quantization, min-notional, and the durable kill switch.

``RealGuard`` is the thin pre-trade boundary (ADR-0017) — it quantizes every
order against its ``InstrumentSpec`` and denies (never sends) anything invalid:
a size that rounds to zero, a below-min-notional order, or any placement while
the kill switch is tripped. ``NoopGuard`` is the passthrough twin (tests/paper)
that keeps the seam real. The kill switch is halt-only and durable (ADR-0026):
persisted through the ``Store`` and restored on construction, so a halt outlives
a crash.
"""

from decimal import Decimal

import structlog.testing

from tickwright.adapters.clock import ManualClock
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    Approved,
    Denied,
    InstrumentSpec,
    OrderType,
    PlaceSignal,
    Side,
    TimeInForce,
)
from tickwright.engine.guard import NoopGuard, RealGuard


def _spec(
    *,
    sz_decimals: int = 3,
    max_decimals: int = 6,
    max_sig_figs: int | None = None,
    min_notional: str = "0",
) -> InstrumentSpec:
    return InstrumentSpec(
        symbol="BTC",
        sz_decimals=sz_decimals,
        max_decimals=max_decimals,
        max_sig_figs=max_sig_figs,
        min_notional=Decimal(min_notional),
    )


def _limit_signal(
    *,
    price: str = "100",
    quantity: str = "1",
    side: Side = Side.BUY,
    seq: int = 1,
) -> PlaceSignal:
    return PlaceSignal(
        ts_event=1_000,
        ts_init=1_000,
        strategy_id="trivial",
        symbol="BTC",
        seq=seq,
        side=side,
        quantity=Decimal(quantity),
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        price=Decimal(price),
    )


def _guard(spec: InstrumentSpec | None = None, *, store: SQLiteStore | None = None) -> RealGuard:
    return RealGuard(
        specs={"BTC": spec or _spec()},
        store=store or SQLiteStore(":memory:"),
        clock=ManualClock(start_ns=1_000),
    )


def test_approves_a_valid_limit_with_quantized_size_and_price() -> None:
    guard = _guard(_spec(sz_decimals=3, max_decimals=6))
    decision = guard.check(_limit_signal(price="100.1237", quantity="1.23456", side=Side.BUY))
    assert isinstance(decision, Approved)
    # Size rounds down to sz_decimals; a BUY price rounds down (passive side).
    assert decision.quantity == Decimal("1.234")
    assert decision.price == Decimal("100.123")


def test_denies_a_size_that_rounds_to_zero() -> None:
    guard = _guard(_spec(sz_decimals=3))
    # 0.0004 below the 0.001 size tick quantizes to zero — a phantom order that
    # must never be sent (ADR-0017). DENIED, with a reason.
    decision = guard.check(_limit_signal(quantity="0.0004"))
    assert isinstance(decision, Denied)
    assert decision.reason


def test_denies_a_limit_below_min_notional() -> None:
    # notional = quantized price × quantized size = 100 × 0.05 = 5, below the
    # min_notional of 10. The guard has the exact price (LIMIT) so it denies
    # locally — never sent (contrast the venue-adjudicated MARKET path).
    guard = _guard(_spec(sz_decimals=3, min_notional="10"))
    decision = guard.check(_limit_signal(price="100", quantity="0.05"))
    assert isinstance(decision, Denied)
    assert decision.reason


def test_approves_a_limit_at_or_above_min_notional() -> None:
    # notional = 100 × 0.2 = 20, at/above min_notional 10 → approved.
    guard = _guard(_spec(sz_decimals=3, min_notional="10"))
    assert isinstance(guard.check(_limit_signal(price="100", quantity="0.2")), Approved)


def test_tripped_kill_switch_denies_every_new_placement() -> None:
    guard = _guard()
    assert isinstance(guard.check(_limit_signal()), Approved)  # ok before the halt

    guard.trip_kill_switch("manual halt")

    assert guard.kill_switch_tripped
    decision = guard.check(_limit_signal(seq=2))
    assert isinstance(decision, Denied)
    assert decision.reason


def test_reset_kill_switch_re_enables_placement() -> None:
    guard = _guard()
    guard.trip_kill_switch("manual halt")
    guard.reset_kill_switch()

    assert not guard.kill_switch_tripped
    assert isinstance(guard.check(_limit_signal()), Approved)


def test_tripped_kill_switch_is_restored_on_a_fresh_guard() -> None:
    # A halt must outlive the process that set it (ADR-0026): trip one guard,
    # then build a fresh guard over the same durable store — it comes back
    # tripped, and still denies. Fail-safe: a crash never silently un-halts.
    store = SQLiteStore(":memory:")
    _guard(store=store).trip_kill_switch("halt before crash")

    revived = _guard(store=store)

    assert revived.kill_switch_tripped
    assert isinstance(revived.check(_limit_signal()), Denied)


def test_reset_is_restored_on_a_fresh_guard() -> None:
    # The other direction: an explicit reset is durable too, so a revived guard
    # does not re-halt on a stale tripped record.
    store = SQLiteStore(":memory:")
    first = _guard(store=store)
    first.trip_kill_switch("halt")
    first.reset_kill_switch()

    revived = _guard(store=store)

    assert not revived.kill_switch_tripped
    assert isinstance(revived.check(_limit_signal()), Approved)


def test_trip_and_reset_emit_named_events() -> None:
    # The kill switch is observable telemetry, not silent (ADR-0020/0026): each
    # trip/reset emits its named event, the trip carrying the operator's reason.
    guard = _guard()
    with structlog.testing.capture_logs() as logs:
        guard.trip_kill_switch("market data looks wrong")
        guard.reset_kill_switch()

    events = [log["event"] for log in logs]
    assert "guard.kill_switch_tripped" in events
    assert "guard.kill_switch_reset" in events
    tripped = next(log for log in logs if log["event"] == "guard.kill_switch_tripped")
    assert tripped["reason"] == "market data looks wrong"


def test_noop_guard_passes_a_signal_through_unmodified() -> None:
    # The passthrough twin: no quantization, always approved — the seam is real.
    guard = NoopGuard()
    decision = guard.check(_limit_signal(price="100.1237", quantity="1.23456"))
    assert isinstance(decision, Approved)
    assert decision.quantity == Decimal("1.23456")
    assert decision.price == Decimal("100.1237")
    # Its kill switch is inert: trip/reset are no-ops, so it never halts.
    guard.trip_kill_switch("ignored")
    assert not guard.kill_switch_tripped
