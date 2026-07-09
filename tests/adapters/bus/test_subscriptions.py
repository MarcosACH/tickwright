"""``Subscriptions`` — the type-guarded, registration-ordered fan-out (ADR-0023).

Both ``EventBus`` backends delegate their ``subscribe`` and per-event dispatch
here, so parity requires this be *identical* across backends — it is one module
they share, not two copies that must agree. These tests pin the contract at its
own interface: matching by ``isinstance`` (so a base-family subscription catches
subclasses), delivery in registration order, and exceptions propagating to the
caller (each backend wraps that propagation in its own containment).
"""

import asyncio
from decimal import Decimal

import pytest

from tickwright.adapters.bus.subscriptions import Subscriptions
from tickwright.domain import MarketTick, OrderType, PlaceSignal, Side, Signal, TimeInForce
from tickwright.domain.enums import AggressorSide


def _tick(seq: int = 1) -> MarketTick:
    return MarketTick(
        ts_event=seq,
        ts_init=seq,
        symbol="BTC",
        price=Decimal("100"),
        size=Decimal("1"),
        aggressor_side=AggressorSide.BUY,
        trade_id=f"t{seq}",
        seq=seq,
    )


def _signal(seq: int = 1) -> PlaceSignal:
    return PlaceSignal(
        ts_event=seq,
        ts_init=seq,
        strategy_id="s",
        symbol="BTC",
        seq=seq,
        side=Side.BUY,
        quantity=Decimal("1"),
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.IOC,
    )


def test_dispatch_delivers_to_subscribers_of_the_matching_type() -> None:
    subs = Subscriptions()
    seen: list[MarketTick] = []
    subs.subscribe(MarketTick, lambda ev: _record(seen, ev))

    tick = _tick()
    asyncio.run(subs.dispatch(tick))

    assert seen == [tick]


def test_dispatch_skips_subscribers_of_a_different_type() -> None:
    subs = Subscriptions()
    seen: list[object] = []
    subs.subscribe(MarketTick, lambda ev: _record(seen, ev))

    asyncio.run(subs.dispatch(_signal()))

    assert seen == []


def test_subscribing_to_a_base_family_catches_subclasses() -> None:
    # ExecutionManager subscribes to the Signal base to catch PlaceSignal.
    subs = Subscriptions()
    seen: list[Signal] = []
    subs.subscribe(Signal, lambda ev: _record(seen, ev))

    signal = _signal()
    asyncio.run(subs.dispatch(signal))

    assert seen == [signal]


def test_dispatch_runs_subscribers_in_registration_order() -> None:
    subs = Subscriptions()
    order: list[str] = []
    subs.subscribe(MarketTick, lambda ev: _append(order, "first"))
    subs.subscribe(MarketTick, lambda ev: _append(order, "second"))

    asyncio.run(subs.dispatch(_tick()))

    assert order == ["first", "second"]


def test_a_handler_exception_propagates_out_of_dispatch() -> None:
    # Each backend relies on this: the in-memory bus lets it unwind into the
    # publish that caused it; the Kafka poll loop catches it for the draining
    # publisher. Both need dispatch to raise rather than swallow.
    subs = Subscriptions()

    async def brittle(_: MarketTick) -> None:
        raise RuntimeError("boom")

    subs.subscribe(MarketTick, brittle)

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(subs.dispatch(_tick()))


def test_dispatch_iterates_a_snapshot_so_a_handler_may_subscribe_mid_dispatch() -> None:
    # Dispatch iterates a snapshot of the registry, so a handler subscribing
    # during dispatch neither mutates the live iteration nor is delivered to
    # within the same dispatch — it takes effect on the next event only.
    subs = Subscriptions()
    late: list[str] = []

    async def subscribe_a_latecomer(_: MarketTick) -> None:
        subs.subscribe(MarketTick, lambda ev: _append(late, "latecomer"))

    subs.subscribe(MarketTick, subscribe_a_latecomer)

    asyncio.run(subs.dispatch(_tick()))
    assert late == []  # the latecomer did not fire on the dispatch that added it

    asyncio.run(subs.dispatch(_tick(2)))
    assert late == ["latecomer"]  # it fires on the next event


# ---- helpers ---------------------------------------------------------------


async def _record(sink: list, event: object) -> None:
    sink.append(event)


async def _append(sink: list[str], label: str) -> None:
    sink.append(label)
