"""``PaperExchange`` + ``ImmediateFillModel`` (ADR-0012 / ADR-0027).

The paper exchange caches the latest tick per symbol and fills a MARKET order on
receipt against that tick's price, emitting a raw ``FillReport`` on the bus (the
exchange owns no saga — ADR-0015). The default fill model is deterministic,
optimistic, zero-slippage, full-fill: no RNG at all.
"""

import asyncio
from decimal import Decimal

import pytest

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.domain import (
    AggressorSide,
    FillReport,
    InstrumentSpec,
    MarketTick,
    Netting,
    OrderState,
    OrderStatusReport,
    OrderType,
    PlaceOrder,
    Side,
    TimeInForce,
    VenueOrderView,
)

# The paper account's opening cash. The venue requires it (ADR-0042 §1: the
# engine supplies no collateral of its own); these tests do not exercise the
# ledger, so one shared declaration keeps every wiring site honest and quiet.
_GENESIS = Decimal("100000")


def _tick(price: str, ts: int = 1_000, symbol: str = "BTC") -> MarketTick:
    return MarketTick(
        ts_event=ts,
        ts_init=ts,
        symbol=symbol,
        price=Decimal(price),
        size=Decimal("5"),
        aggressor_side=AggressorSide.BUY,
        trade_id=f"t{ts}",
        seq=0,
    )


def _market_order(qty: str = "1", symbol: str = "BTC", cloid: str = "0xabc") -> PlaceOrder:
    return PlaceOrder(
        cloid=cloid,
        symbol=symbol,
        side=Side.BUY,
        quantity=Decimal(qty),
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.IOC,
    )


def _limit_order(
    price: str,
    *,
    time_in_force: TimeInForce = TimeInForce.GTC,
    post_only: bool = False,
    side: Side = Side.BUY,
    qty: str = "1",
    symbol: str = "BTC",
    cloid: str = "0xabc",
) -> PlaceOrder:
    return PlaceOrder(
        cloid=cloid,
        symbol=symbol,
        side=side,
        quantity=Decimal(qty),
        order_type=OrderType.LIMIT,
        time_in_force=time_in_force,
        price=Decimal(price),
        post_only=post_only,
    )


def _harness() -> tuple[PaperExchange, InMemoryBus, ManualClock, list[FillReport]]:
    bus = InMemoryBus()
    clock = ManualClock()
    exchange = PaperExchange(
        bus=bus, clock=clock, fill_model=ImmediateFillModel(), genesis_collateral=_GENESIS
    )
    reports: list[FillReport] = []
    bus.subscribe(FillReport, lambda r: _record(reports, r))
    return exchange, bus, clock, reports


def test_the_paper_venue_consumes_ticks_off_its_bus_with_no_external_wiring() -> None:
    """A ``PaperExchange`` is *defined* by filling off the tick stream, so it
    subscribes itself to ``MarketTick`` at construction (ADR-0012): the
    composition root and every test get a tradable venue with no follow-up
    wiring line to repeat or forget. Proof: a tick published with **no** manual
    ``subscribe`` still reaches the venue — a MARKET then fills at the cached
    price, which is only possible if the venue saw the tick."""
    bus = InMemoryBus()
    clock = ManualClock()
    exchange = PaperExchange(
        bus=bus, clock=clock, fill_model=ImmediateFillModel(), genesis_collateral=_GENESIS
    )
    fills: list[FillReport] = []
    bus.subscribe(FillReport, lambda r: _record(fills, r))

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("42000"))
        await exchange.place(_market_order(qty="0.5"))

    asyncio.run(scenario())

    assert len(fills) == 1
    assert fills[0].price == Decimal("42000")


def _limit_harness() -> tuple[
    PaperExchange, InMemoryBus, ManualClock, list[FillReport], list[OrderStatusReport]
]:
    exchange, bus, clock, fills = _harness()
    statuses: list[OrderStatusReport] = []
    bus.subscribe(OrderStatusReport, lambda r: _record(statuses, r))
    return exchange, bus, clock, fills, statuses


_BTC_SPEC = InstrumentSpec(
    symbol="BTC",
    sz_decimals=3,
    max_decimals=6,
    max_sig_figs=5,
    min_notional=Decimal("10"),
)


def _specced_harness() -> tuple[
    PaperExchange, InMemoryBus, ManualClock, list[FillReport], list[OrderStatusReport]
]:
    bus = InMemoryBus()
    clock = ManualClock()
    exchange = PaperExchange(
        bus=bus,
        clock=clock,
        fill_model=ImmediateFillModel(),
        genesis_collateral=_GENESIS,
        instrument_specs={"BTC": _BTC_SPEC},
    )
    fills: list[FillReport] = []
    statuses: list[OrderStatusReport] = []
    bus.subscribe(FillReport, lambda r: _record(fills, r))
    bus.subscribe(OrderStatusReport, lambda r: _record(statuses, r))
    return exchange, bus, clock, fills, statuses


def test_instrument_specs_exposes_the_configured_specs() -> None:
    # Adapter-sourced specs (ADR-0031): the paper exchange authors them from
    # config and exposes them for the Engine to wire into the guard.
    exchange, *_ = _specced_harness()
    assert exchange.instrument_specs() == {"BTC": _BTC_SPEC}


def test_market_order_below_min_notional_is_rejected_by_the_venue() -> None:
    exchange, bus, clock, fills, statuses = _specced_harness()

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("100"))
        # notional = 100 × 0.05 = 5, below min_notional 10. Only the venue knows
        # a MARKET's fill price, so it adjudicates here → REJECTED, not filled.
        await exchange.place(_market_order(qty="0.05"))

    asyncio.run(scenario())

    assert fills == []  # never filled
    assert len(statuses) == 1
    assert statuses[0].status is OrderState.REJECTED
    assert statuses[0].reason


def test_market_order_at_or_above_min_notional_fills_normally() -> None:
    exchange, bus, clock, fills, statuses = _specced_harness()

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("100"))
        await exchange.place(_market_order(qty="0.2"))  # notional 20 ≥ 10

    asyncio.run(scenario())

    assert len(fills) == 1
    assert not [s for s in statuses if s.status is OrderState.REJECTED]


def test_market_order_fills_at_the_latest_tick_price() -> None:
    exchange, bus, clock, reports = _harness()

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("42000"))
        await exchange.place(_market_order(qty="0.5"))

    asyncio.run(scenario())

    assert len(reports) == 1
    report = reports[0]
    assert report.cloid == "0xabc"
    assert report.price == Decimal("42000")
    assert report.quantity == Decimal("0.5")  # full fill, no partial
    assert report.symbol == "BTC"


def test_market_order_uses_the_most_recent_tick() -> None:
    exchange, bus, clock, reports = _harness()

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("100", ts=1_000))
        clock.advance_to(2_000)
        await bus.publish(_tick("101", ts=2_000))
        await exchange.place(_market_order())

    asyncio.run(scenario())

    assert reports[0].price == Decimal("101")


def test_fill_report_is_stamped_from_the_clock() -> None:
    exchange, bus, clock, reports = _harness()

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("100"))
        clock.advance_to(7_500)
        await exchange.place(_market_order())

    asyncio.run(scenario())

    assert reports[0].ts_event == 7_500
    assert reports[0].ts_init == 7_500


def test_fill_trade_id_is_deterministic_across_runs() -> None:
    def run() -> str:
        exchange, bus, clock, reports = _harness()

        async def scenario() -> None:
            clock.advance_to(1_000)
            await bus.publish(_tick("100"))
            await exchange.place(_market_order())

        asyncio.run(scenario())
        return reports[0].trade_id

    assert run() == run()


def test_market_order_without_a_cached_tick_is_rejected() -> None:
    exchange, _, _, _ = _harness()
    # No tick has been published, so there is no price to fill against.
    with pytest.raises(ValueError, match="no market tick"):
        asyncio.run(exchange.place(_market_order()))


def test_gtc_limit_that_does_not_cross_rests_without_filling() -> None:
    exchange, bus, clock, fills, statuses = _limit_harness()

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("42000"))
        # A BUY LIMIT at 41000 is below the market (42000): it cannot fill now, so
        # it rests on the book and the venue reports it working (LIVE).
        await exchange.place(_limit_order("41000"))

    asyncio.run(scenario())

    assert fills == []
    assert len(statuses) == 1
    assert statuses[0].cloid == "0xabc"
    assert statuses[0].status is OrderState.LIVE


def test_resting_limit_fills_when_a_later_tick_crosses_its_price() -> None:
    exchange, bus, clock, fills, statuses = _limit_harness()

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("42000"))
        await exchange.place(_limit_order("41000"))  # rests below the market
        # The market falls to the limit: the resting BUY now crosses and fills.
        clock.advance_to(2_000)
        await bus.publish(_tick("41000", ts=2_000))

    asyncio.run(scenario())

    assert len(fills) == 1
    assert fills[0].cloid == "0xabc"
    assert fills[0].quantity == Decimal("1")
    assert fills[0].price == Decimal("41000")  # filled at its limit price


def test_resting_sell_limit_fills_when_a_later_tick_rises_to_its_price() -> None:
    exchange, bus, clock, fills, statuses = _limit_harness()

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("42000"))
        # A SELL LIMIT at 43000 is above the market (42000): it cannot fill now, so
        # it rests. When the market rises to the limit the resting SELL crosses.
        await exchange.place(_limit_order("43000", side=Side.SELL))
        clock.advance_to(2_000)
        await bus.publish(_tick("43000", ts=2_000))

    asyncio.run(scenario())

    assert [s.status for s in statuses] == [OrderState.LIVE]
    assert len(fills) == 1
    assert fills[0].cloid == "0xabc"
    assert fills[0].price == Decimal("43000")  # filled at its limit price


def test_marketable_limit_fills_on_arrival_without_resting() -> None:
    exchange, bus, clock, fills, statuses = _limit_harness()

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("42000"))
        # A BUY LIMIT at 43000 is already above the market: it crosses on arrival
        # and fills immediately, rather than resting.
        await exchange.place(_limit_order("43000"))

    asyncio.run(scenario())

    assert statuses == []  # never rested, so no LIVE report
    assert len(fills) == 1
    assert fills[0].price == Decimal("43000")  # at the limit price
    assert fills[0].quantity == Decimal("1")


def test_ioc_limit_that_does_not_cross_is_cancelled_on_receipt() -> None:
    exchange, bus, clock, fills, statuses = _limit_harness()

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("42000"))
        # An IOC never rests: a BUY LIMIT at 41000 that can't fill now is
        # cancelled immediately, not left working on the book.
        await exchange.place(_limit_order("41000", time_in_force=TimeInForce.IOC))
        # A later crossing tick must not fill it — it is already gone.
        clock.advance_to(2_000)
        await bus.publish(_tick("41000", ts=2_000))

    asyncio.run(scenario())

    assert fills == []
    assert len(statuses) == 1
    assert statuses[0].status is OrderState.CANCELLED


def test_post_only_limit_that_would_cross_is_rejected_not_filled() -> None:
    exchange, bus, clock, fills, statuses = _limit_harness()

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("42000"))
        # A post_only BUY LIMIT at 43000 would cross on arrival (take liquidity);
        # post_only forbids that, so the venue rejects it outright.
        await exchange.place(_limit_order("43000", post_only=True))

    asyncio.run(scenario())

    assert fills == []
    assert len(statuses) == 1
    assert statuses[0].status is OrderState.REJECTED


def test_post_only_limit_that_does_not_cross_rests_normally() -> None:
    exchange, bus, clock, fills, statuses = _limit_harness()

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("42000"))
        # post_only only forbids crossing; a maker order that rests is fine.
        await exchange.place(_limit_order("41000", post_only=True))

    asyncio.run(scenario())

    assert fills == []
    assert [s.status for s in statuses] == [OrderState.LIVE]


def test_cancel_removes_a_resting_order_and_reports_cancelled() -> None:
    exchange, bus, clock, fills, statuses = _limit_harness()

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("42000"))
        await exchange.place(_limit_order("41000"))  # rests
        statuses.clear()  # drop the LIVE report; we only care about the cancel
        await exchange.cancel("0xabc")
        # After cancel it is off the book: a later crossing tick must not fill it.
        clock.advance_to(2_000)
        await bus.publish(_tick("41000", ts=2_000))

    asyncio.run(scenario())

    assert fills == []
    assert [s.status for s in statuses] == [OrderState.CANCELLED]
    assert statuses[0].cloid == "0xabc"


def test_cancel_of_an_unknown_order_is_a_benign_no_op() -> None:
    exchange, bus, clock, fills, statuses = _limit_harness()

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("42000"))
        # Nothing resting under this cloid (already filled/cancelled or never
        # placed): the venue reports nothing and nothing raises.
        await exchange.cancel("0xdeadbeef")

    asyncio.run(scenario())

    assert fills == []
    assert statuses == []


def test_immediate_fill_model_is_full_fill_zero_slippage() -> None:
    model = ImmediateFillModel()
    fill = asyncio.run(model.market_fill(_market_order(qty="3"), _tick("250")))
    assert fill.quantity == Decimal("3")
    assert fill.price == Decimal("250")


async def _record(sink: list, report: object) -> None:
    sink.append(report)


def test_fetch_order_reports_a_resting_limit_as_live() -> None:
    exchange, bus, _, _, _ = _limit_harness()

    async def scenario() -> VenueOrderView | None:
        await bus.publish(_tick("42000"))
        await exchange.place(_limit_order("41000"))
        # The reconciler's query-shaped read (ADR-0004): venue truth by cloid,
        # never a bus message.
        return await exchange.fetch_order("0xabc")

    view = asyncio.run(scenario())
    assert view is not None  # a paper read can never fail (ADR-0024)
    assert view.status is not None
    assert view.status.status is OrderState.LIVE
    assert view.fills == ()


def test_fetch_order_carries_the_fills_of_a_filled_order() -> None:
    exchange, bus, _, _ = _harness()

    async def scenario() -> VenueOrderView | None:
        await bus.publish(_tick("42000"))
        await exchange.place(_market_order())
        return await exchange.fetch_order("0xabc")

    view = asyncio.run(scenario())
    assert view is not None
    # The fill-history half of the ADR-0011 cross-check: a vanished order that
    # actually filled is provable from the view alone.
    assert view.has_record
    assert [fill.trade_id for fill in view.fills] == ["0xabc-1"]
    assert view.fills[0].quantity == Decimal("1")


def test_fetch_order_for_an_unknown_cloid_is_positive_proof_of_no_record() -> None:
    exchange, _, _, _ = _harness()

    view = asyncio.run(exchange.fetch_order("0xghost"))

    # An empty view, never None: on paper a read cannot fail, and "no record"
    # must stay distinguishable from an outage (ADR-0011 inv 1).
    assert view is not None
    assert not view.has_record
    assert view.status is None and view.fills == ()


def test_the_paper_venue_declares_a_two_segment_account_id_and_its_genesis() -> None:
    """``paper-<label>`` stays unambiguously two segments against a live venue's
    three (ADR-0042 §5), and the operator's declared collateral rides the spec."""
    exchange = PaperExchange(
        bus=InMemoryBus(),
        clock=ManualClock(),
        fill_model=ImmediateFillModel(),
        genesis_collateral=Decimal("250000"),
        account_label="momentum_v2",
    )

    spec = exchange.account_spec()

    assert spec.account_id == "paper-momentum_v2"
    assert spec.genesis_collateral == Decimal("250000")
    assert spec.netting is Netting.NET


def test_the_paper_account_label_defaults() -> None:
    exchange = PaperExchange(
        bus=InMemoryBus(),
        clock=ManualClock(),
        fill_model=ImmediateFillModel(),
        genesis_collateral=_GENESIS,
    )

    assert exchange.account_spec().account_id == "paper-default"
