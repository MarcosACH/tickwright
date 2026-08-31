"""``PaperExchange`` + ``ImmediateFillModel`` (ADR-0012 / ADR-0027).

The paper exchange caches the latest tick per symbol and fills a MARKET order on
receipt against that tick's price, emitting a raw ``FillReport`` on the bus (the
exchange owns no saga — ADR-0015). The default fill model is deterministic,
optimistic, zero-slippage, full-fill: no RNG at all.
"""

import asyncio
import random
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from ledgers import GENESIS
from seam_claims import assert_every_member_is_claimed

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.paper import (
    ImmediateFillModel,
    PaperExchange,
    PaperExchangeConfig,
    StochasticFillModel,
    StochasticParams,
)
from tickwright.domain import (
    AggressorSide,
    Event,
    Exchange,
    FillReport,
    InstrumentSpec,
    LeverageOutOfBounds,
    LeverageSpec,
    MarketTick,
    Netting,
    OrderState,
    OrderStatusReport,
    OrderType,
    PlaceOrder,
    Side,
    TimeInForce,
    VenueAccountState,
    VenueOrderView,
    VenueReadFailure,
)


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
        bus=bus,
        clock=clock,
        fill_model=ImmediateFillModel(),
        genesis_collateral=GENESIS,
        account_net=dict,
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
        bus=bus,
        clock=clock,
        fill_model=ImmediateFillModel(),
        genesis_collateral=GENESIS,
        account_net=dict,
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


# Hyperliquid's base rates, both a **cost**: a maker fill is not a rebate on a
# fresh account (ADR-0036, #152). Sized so the two are told apart by the number,
# and free of a min-notional so nothing else adjudicates the fills below.
_FEE_SPEC = InstrumentSpec(
    symbol="BTC",
    sz_decimals=3,
    max_decimals=6,
    min_notional=Decimal("0"),
    maker_fee=Decimal("0.00015"),
    taker_fee=Decimal("0.00045"),
)


def _specced_harness(
    spec: InstrumentSpec = _BTC_SPEC,
) -> tuple[PaperExchange, InMemoryBus, ManualClock, list[FillReport], list[OrderStatusReport]]:
    """A venue that knows ``spec``'s symbol. Defaulting to ``_BTC_SPEC`` — which
    declares only the quantizer's fields — is what lets the fee tests pass
    ``_FEE_SPEC`` and every other specced test keep asserting a frictionless
    venue against the same wiring."""
    bus = InMemoryBus()
    clock = ManualClock()
    exchange = PaperExchange(
        bus=bus,
        clock=clock,
        fill_model=ImmediateFillModel(),
        genesis_collateral=GENESIS,
        instrument_specs={spec.symbol: spec},
        account_net=dict,
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


def test_a_market_fill_is_taker_and_carries_the_taker_fee() -> None:
    # A MARKET takes liquidity by definition — it fills on arrival, at the price
    # the venue already has — so it pays the taker rate (ADR-0036). 0.5 @ 42 000
    # is a 21 000 notional; 0.045 % of it is 9.45 USDC, charged as a positive cost.
    exchange, bus, clock, fills, _ = _specced_harness(_FEE_SPEC)

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("42000"))
        await exchange.place(_market_order(qty="0.5"))

    asyncio.run(scenario())

    assert [f.fee for f in fills] == [Decimal("9.45")]


def test_a_fill_off_the_resting_book_carries_the_maker_fee() -> None:
    # This order sat on the book and a later tick came to it, so it provided the
    # liquidity (ADR-0036). The model fills a LIMIT at its own price: 1 @ 41 000
    # is a 41 000 notional, and 0.015 % of it is 6.15 USDC — still a cost, a
    # rebate being a volume-tier property rather than a liquidity-side one.
    exchange, bus, clock, fills, _ = _specced_harness(_FEE_SPEC)

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("42000"))
        await exchange.place(_limit_order("41000"))  # uncrossed: rests
        await bus.publish(_tick("40000", ts=2_000))  # crosses it off the book

    asyncio.run(scenario())

    assert [f.fee for f in fills] == [Decimal("6.15")]


def test_a_limit_marketable_on_arrival_carries_the_taker_fee() -> None:
    # It crossed the moment it landed, so it took liquidity exactly as a MARKET
    # does — resting on the book first is bookkeeping for the remainder, not a
    # claim about who provided liquidity (ADR-0036). 1 @ 43 000 at 0.045 % is
    # 19.35, three times the maker charge on the same notional.
    exchange, bus, clock, fills, _ = _specced_harness(_FEE_SPEC)

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("42000"))
        await exchange.place(_limit_order("43000"))  # a BUY above the market: crosses

    asyncio.run(scenario())

    assert [f.fee for f in fills] == [Decimal("19.35")]


def test_a_post_only_fill_is_always_a_maker_fill() -> None:
    # ``post_only`` is the maker-only guarantee, and the venue keeps it
    # structurally rather than by consulting the flag at the fee boundary: one
    # that would cross on arrival is rejected before any fill exists, so the only
    # path a post_only fill can reach is the resting book's (ADR-0036).
    exchange, bus, clock, fills, statuses = _specced_harness(_FEE_SPEC)

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("42000"))
        await exchange.place(_limit_order("41000", post_only=True))  # uncrossed: rests
        await bus.publish(_tick("40000", ts=2_000))

    asyncio.run(scenario())

    assert not [s for s in statuses if s.status is OrderState.REJECTED]
    assert [f.fee for f in fills] == [Decimal("6.15")]  # 41 000 × 0.015 %


def test_one_order_filling_on_arrival_and_again_off_the_book_pays_both_rates() -> None:
    """The aggressor bit is a property of the **fill**, never of the order.

    Every boundary above covers an order whose legs are all one side, so deciding
    maker/taker once per order — stashing it when the order rests, or reading
    ``post_only`` — would satisfy all four while being wrong. This is the order
    that tells the two apart: it crosses on arrival and *takes*, the model fills
    only part of it, and the remainder then sits on the book and *provides* the
    liquidity a later tick comes for. One order, one price, three fills, two
    rates (ADR-0036).
    """
    bus = InMemoryBus()
    clock = ManualClock()
    exchange = PaperExchange(
        bus=bus,
        clock=clock,
        # The partial fill on arrival is the whole point and ``ImmediateFillModel``
        # is full-fill, so the remainder this test turns on would never exist.
        # Certain fills and no slippage leave the RNG deciding nothing asserted
        # below: the model offers 40 % of the order's *own* quantity every time,
        # and the book caps the last leg at the 0.2 that was still working.
        fill_model=StochasticFillModel(
            rng=random.Random(11),
            clock=clock,
            params=StochasticParams(
                prob_slippage=0.0,
                max_slippage=Decimal("0"),
                prob_fill_on_limit=1.0,
                partial_fill_fraction=Decimal("0.4"),
            ),
        ),
        genesis_collateral=GENESIS,
        instrument_specs={"BTC": _FEE_SPEC},
        account_net=dict,
    )
    fills: list[FillReport] = []
    bus.subscribe(FillReport, lambda r: _record(fills, r))

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("42000"))
        await exchange.place(_limit_order("43000"))  # a BUY above the market: crosses
        # Still crossing — but the order is now the side that was already there,
        # so these two legs are made, not taken.
        await bus.publish(_tick("41000", ts=2_000))
        await bus.publish(_tick("41000", ts=3_000))

    asyncio.run(scenario())

    # Every leg fills at the order's own 43 000, so nothing but the rate can move
    # the fee: 0.4 × 43 000 = 17 200 is 7.74 at 0.045 % and 2.58 at 0.015 %, and
    # the closing 0.2's 8 600 is 1.29 at 0.015 %.
    assert [(f.quantity, f.price) for f in fills] == [
        (Decimal("0.4"), Decimal("43000")),
        (Decimal("0.4"), Decimal("43000")),
        (Decimal("0.2"), Decimal("43000")),
    ]
    assert [f.fee for f in fills] == [Decimal("7.74"), Decimal("2.58"), Decimal("1.29")]


def test_a_spec_with_default_rates_charges_nothing() -> None:
    # The frictionless-spec guarantee (ADR-0036): fees default to zero, so every
    # configuration authored before they existed keeps producing exactly the
    # outcomes it did. ``_BTC_SPEC`` declares only the quantizer's fields.
    exchange, bus, clock, fills, _ = _specced_harness()

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("42000"))
        await exchange.place(_market_order(qty="0.5"))

    asyncio.run(scenario())

    assert [f.fee for f in fills] == [Decimal("0")]


def test_a_symbol_with_no_spec_at_all_charges_nothing() -> None:
    # A spec is what an operator adds to *model* venue friction, not a
    # precondition for filling: this venue already trades an unspecced symbol
    # (min-notional is skipped the same way), so an absent spec charges zero
    # rather than faulting the fill.
    exchange, bus, clock, fills = _harness()

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("42000"))
        await exchange.place(_market_order(qty="0.5"))

    asyncio.run(scenario())

    assert [f.fee for f in fills] == [Decimal("0")]


def test_configured_rates_change_no_fill_the_matching_path_produces() -> None:
    """The no-smear rule, asserted against the one model that could reveal a
    breach (ADR-0013 affirmed by ADR-0036).

    The fee is computed *after* matching, so the rates may not touch which
    quantity fills, at what price, or on which tick. A seeded
    ``StochasticFillModel`` is what makes that checkable: every decision it makes
    comes from its RNG, so a fee computation that reached the matching path —
    consuming a draw, or reading a price through the rate — would desynchronize
    the two runs and diverge the sequences. Under ``ImmediateFillModel`` the
    prices are fixed and the comparison would pass with the rule broken.
    """

    def sequence(spec: InstrumentSpec) -> list[tuple[Decimal, Decimal]]:
        bus = InMemoryBus()
        clock = ManualClock()
        exchange = PaperExchange(
            bus=bus,
            clock=clock,
            fill_model=StochasticFillModel(
                rng=random.Random(11),
                clock=clock,
                params=StochasticParams(
                    prob_slippage=1.0,
                    max_slippage=Decimal("0.001"),
                    prob_fill_on_limit=0.6,
                    partial_fill_fraction=Decimal("0.4"),
                ),
            ),
            genesis_collateral=GENESIS,
            instrument_specs={"BTC": spec},
            account_net=dict,
        )
        fills: list[FillReport] = []
        bus.subscribe(FillReport, lambda r: _record(fills, r))

        async def scenario() -> None:
            clock.advance_to(1_000)
            await bus.publish(_tick("42000"))
            await exchange.place(_market_order(qty="0.5"))
            await exchange.place(_limit_order("41000", cloid="0xrest"))
            for step, price in enumerate(("41500", "40900", "40800", "40950"), start=2):
                await bus.publish(_tick(price, ts=step * 1_000))

        asyncio.run(scenario())
        return [(f.quantity, f.price) for f in fills]

    charged = sequence(_FEE_SPEC)
    frictionless = sequence(
        replace(_FEE_SPEC, maker_fee=Decimal("0"), taker_fee=Decimal("0")),
    )

    assert charged == frictionless
    assert len(charged) > 1  # the scenario really did exercise the resting book


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

    async def scenario() -> VenueOrderView | VenueReadFailure:
        await bus.publish(_tick("42000"))
        await exchange.place(_limit_order("41000"))
        # The reconciler's query-shaped read (ADR-0004): venue truth by cloid,
        # never a bus message.
        return await exchange.fetch_order("0xabc")

    view = asyncio.run(scenario())
    # A view, never a `VenueReadFailure`: a paper read can never fail (ADR-0024).
    assert isinstance(view, VenueOrderView)
    assert view.status is not None
    assert view.status.status is OrderState.LIVE
    assert view.fills == ()


def test_fetch_order_carries_the_fills_of_a_filled_order() -> None:
    exchange, bus, _, _ = _harness()

    async def scenario() -> VenueOrderView | VenueReadFailure:
        await bus.publish(_tick("42000"))
        await exchange.place(_market_order())
        return await exchange.fetch_order("0xabc")

    view = asyncio.run(scenario())
    assert isinstance(view, VenueOrderView)
    # The fill-history half of the ADR-0011 cross-check: a vanished order that
    # actually filled is provable from the view alone.
    assert view.has_record
    assert [fill.trade_id for fill in view.fills] == ["0xabc-1"]
    assert view.fills[0].quantity == Decimal("1")


def test_fetch_order_for_an_unknown_cloid_is_positive_proof_of_no_record() -> None:
    exchange, _, _, _ = _harness()

    view = asyncio.run(exchange.fetch_order("0xghost"))

    # An empty view, never a failure: on paper a read cannot fail, and "no
    # record" must stay distinguishable from an outage (ADR-0011 inv 1).
    assert isinstance(view, VenueOrderView)
    assert not view.has_record
    assert view.status is None and view.fills == ()


def test_the_paper_venue_reports_no_account_truth_even_on_a_healthy_read() -> None:
    """``None`` **always — by construction, not by failure**.

    This venue holds resting orders, per-cloid fill reports and the latest tick,
    and no position, cash or equity state at all (ADR-0043 §4) — so there is no
    account truth here to answer with, and no state of the book changes that.
    The read is taken on a *fully exercised* venue — a tick in, an order placed,
    a fill emitted — precisely so the claim cannot be read as "nothing has
    happened yet".

    ``None`` is the only answer that stays fail-closed under every wiring,
    including a future one that mistakenly points the reconcile cadence at
    paper: it freezes and heals nothing (ADR-0011 inv 1). A zero-filled
    ``VenueAccountState`` would be fail-*open* — the fabricated flat ADR-0034
    forbids — and would heal a restored ledger down to flat. Same contract as a
    failed live read (*no truth to compare against ⇒ never heal*), reached by a
    different route.
    """
    exchange, bus, clock, fills, _statuses = _specced_harness()

    async def scenario() -> tuple[VenueAccountState | None, VenueAccountState | None]:
        cold = await exchange.fetch_account_state()
        clock.advance_to(1_000)
        await bus.publish(_tick("100"))
        await exchange.place(_market_order(qty="0.2"))
        return cold, await exchange.fetch_account_state()

    cold, healthy = asyncio.run(scenario())

    assert len(fills) == 1  # the book really did work
    assert cold is None
    assert healthy is None


def test_the_paper_venue_declares_a_two_segment_account_id_and_its_genesis() -> None:
    """``paper-<label>`` stays unambiguously two segments against a live venue's
    three (ADR-0042 §5), and the operator's declared collateral rides the spec."""
    exchange = PaperExchange(
        bus=InMemoryBus(),
        clock=ManualClock(),
        fill_model=ImmediateFillModel(),
        genesis_collateral=Decimal("250000"),
        account_label="momentum_v2",
        account_net=dict,
    )

    spec = exchange.account_spec()

    assert spec.account_id == "paper-momentum_v2"
    assert spec.genesis_collateral == Decimal("250000")
    assert spec.netting is Netting.NET


def test_the_paper_account_label_defaults_to_the_same_label_the_config_does() -> None:
    """An unlabelled exchange and an unlabelled config must open the *same*
    ledger. The two defaults are one constant precisely so they cannot drift —
    a label decides the ``paper-<label>`` id, so two spellings of "the default"
    would silently point a directly-constructed exchange and a
    ``build_engine``-constructed one at different account histories.
    """
    exchange = PaperExchange(
        bus=InMemoryBus(),
        clock=ManualClock(),
        fill_model=ImmediateFillModel(),
        genesis_collateral=GENESIS,
        account_net=dict,
    )

    assert exchange.account_spec().account_id == "paper-default"
    assert exchange.account_spec().account_id == f"paper-{PaperExchangeConfig().account_label}"


def test_start_connects_nothing_and_leaves_the_one_loop_to_the_supervised_half() -> None:
    """The three verbs the runner drives (ADR-0024 step 4, the ``TaskGroup``, the
    reverse shutdown). There is nothing to *connect* in-process — this venue's
    one link, the ``MarketTick`` subscription, is wired at construction — and
    there is exactly one thing to **run**: ADR-0037's funding generator, which a
    real venue would push and paper has to settle itself.

    **``start()`` spawns nothing**, and that emptiness is the claim rather than
    an absence of work. A loop spawned there is a loop the runner does not
    supervise, so its one failure mode — a refused ledger write — kills it alone
    while the engine runs on accruing nothing (#226). The loop belongs to
    ``run()``, whose task the runner creates and owns; here that ownership is
    stood in for by creating and cancelling it in the same place.

    Driven *alone*, with no tick and no order, and watched the two ways this test
    has always watched. Dispatch is ``isinstance``-guarded (ADR-0023), so a
    subscription to ``Event`` itself sees **everything** this venue publishes,
    including a type that does not exist yet — and it stays empty, because the
    generator is parked on a boundary no advancing clock has reached. The running
    task is what gives its arrival away: a generator publishes no fill, so a
    fill-watcher would have survived it silently, and it publishes nothing at all
    until time advances, so even the catch-all would.

    So the count is asserted rather than the emptiness: **one** task while the
    supervised half runs, so a second loop cannot be added unnoticed, and
    **none** surviving the cancellation, which is the half the bus drain depends
    on — a generator that outlived the teardown's slot would keep publishing into
    the drain and the cascade would never quiesce."""
    exchange, bus, _clock, _fills = _harness()
    published: list[Event] = []
    bus.subscribe(Event, lambda event: _record(published, event))
    after_start: list[asyncio.Task[object]] = []
    while_running: list[asyncio.Task[object]] = []
    surviving: list[asyncio.Task[object]] = []

    def _others() -> list[asyncio.Task[object]]:
        return [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]

    async def scenario() -> None:
        await exchange.start()
        after_start.extend(_others())
        running = asyncio.create_task(exchange.run())
        await asyncio.sleep(0)
        while_running.extend(_others())
        await exchange.stop()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        surviving.extend(task for task in _others() if not task.done())

    asyncio.run(scenario())

    assert published == []
    assert after_start == []  # nothing the runner would not be supervising
    assert len(while_running) == 1  # the funding generator, and nothing else
    assert surviving == []


def test_the_paper_venue_releases_without_a_start_and_keeps_its_book() -> None:
    """The faulted teardown walks the same ordered membership as the graceful
    one, so ``stop()`` is reached after a ``start()`` that refused — or that
    never ran at all, an earlier step having faulted first. Releasing must
    therefore be safe on a venue that never started, and it is a *release*, not
    a reset: a resting order is still the venue's truth afterwards, which is
    what lets restart reconciliation re-adopt it by cloid (ADR-0024).

    Driven **twice**, because one shutdown can drive it twice: a graceful step
    that raises *behind* the venue release faults the run, and the best-effort
    pass re-walks the membership from the top (the runner's half of this claim
    is ``test_a_graceful_teardown_that_breaks_releases_the_venue_a_second_time``
    — asserted there on a double, and here on the adapter itself). Trivially
    true while this body releases nothing, and it stayed that way: ADR-0037's
    funding generator was the one candidate for giving this a task to cancel,
    and the task went to the runner instead (``Exchange.run()``, #226), so what
    an in-process venue releases here is still nothing. The book survives both
    calls, so the second release is no more a reset than the first."""
    exchange, bus, clock, fills, statuses = _limit_harness()

    async def scenario() -> VenueOrderView | VenueReadFailure:
        clock.advance_to(1_000)
        await bus.publish(_tick("42000"))
        await exchange.place(_limit_order("41000"))  # rests, uncrossed
        await exchange.stop()  # no start() ever ran
        await exchange.stop()  # and again: the faulted pass re-walks the membership
        return await exchange.fetch_order("0xabc")

    view = asyncio.run(scenario())

    assert fills == []
    assert isinstance(view, VenueOrderView)
    assert view.status is not None
    assert view.status.status is OrderState.LIVE


def test_the_released_paper_venue_still_answers_a_place_and_a_cancel() -> None:
    """Ahead of the bus drain is not ahead of the last caller. ``stop()`` runs
    before the drain, but the drain is what dispatches the cascade it is waiting
    on, and the strategies are still subscribed behind it — so an in-flight tick
    can still reach one, and its ``Signal`` still reaches the
    ``ExecutionManager``, which calls ``place`` on an adapter already released.
    The seam therefore requires both order verbs to stay **answerable** across
    the release: refusing cleanly rather than raising, and above all never
    hanging, because the drain is blocked on the very cascade they are in.

    ``asyncio.wait_for`` is the "never hanging" half as an *assertion* — without
    a bound, a release that wedged an order verb would hang the suite instead of
    failing it, which is not a result.

    In-process the release holds nothing, so answering here is answering
    normally: the cancel still reports ``CANCELLED`` and the late order still
    fills off the cached tick. That is the claim's shape today; when ADR-0037's
    funding generator gives ``stop()`` something to tear down, this is the test
    that says the teardown may not take the order path down with it."""
    exchange, bus, clock, fills, statuses = _limit_harness()

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("42000"))
        await exchange.place(_limit_order("41000"))  # rests, uncrossed
        await exchange.stop()
        # Behind the release, inside the drain the runner has not reached yet.
        await asyncio.wait_for(exchange.cancel("0xabc"), timeout=5)
        await asyncio.wait_for(exchange.place(_market_order(qty="0.5", cloid="0xlate")), timeout=5)

    asyncio.run(scenario())

    assert [s.status for s in statuses] == [OrderState.LIVE, OrderState.CANCELLED]
    assert [f.cloid for f in fills] == ["0xlate"]


def test_the_paper_venue_satisfies_the_exchange_seam() -> None:
    """Conformance asserted at the adapter, as both bus adapters assert theirs.

    ``Exchange`` is ``runtime_checkable``, so this is precisely a member-presence
    check: a member added to the Protocol fails here for whichever adapter was
    left behind, without anyone maintaining a transcribed list of what the seam
    contains. The half it cannot see — a member every adapter implements but no
    test asserts — is what ``_SEAM_CLAIMS`` below covers.
    """
    exchange, _bus, _clock, _reports = _harness()

    assert isinstance(exchange, Exchange)


# Which test claims each ``Exchange`` member for *this* adapter. Not a second
# copy of the seam: the gate below asserts it against the Protocol itself, so a
# new member cannot arrive without someone naming what asserts it here.
_SEAM_CLAIMS = {
    "start": "test_start_connects_nothing_and_leaves_the_one_loop_to_the_supervised_half",
    "run": "test_a_jump_across_three_boundaries_accrues_three_separate_payments",
    "stop": "test_the_paper_venue_releases_without_a_start_and_keeps_its_book",
    "place": "test_market_order_fills_at_the_latest_tick_price",
    "cancel": "test_cancel_removes_a_resting_order_and_reports_cancelled",
    "fetch_order": "test_fetch_order_reports_a_resting_limit_as_live",
    "fetch_account_state": "test_the_paper_venue_reports_no_account_truth_even_on_a_healthy_read",
    "account_spec": "test_the_paper_venue_declares_a_two_segment_account_id_and_its_genesis",
    "instrument_specs": "test_instrument_specs_exposes_the_configured_specs",
}


def test_every_exchange_member_carries_a_claim_in_the_paper_suite() -> None:
    """The completeness gate the ``isinstance`` check above cannot be.

    Both adapters must implement a new seam member for the engine to run at all,
    so conformance passes the moment the member exists — while nothing asserts
    what it *does* on this venue. ``fetch_account_state`` (#177) is the first to
    arrive against it, and more are queued (#173, #174); each arrives cheaper
    against a gate that names the omission than against a reviewer who has to
    notice it."""
    assert_every_member_is_claimed(Exchange, _SEAM_CLAIMS, suite=Path(__file__).parent)


def test_a_leverage_above_the_instruments_cap_refuses_to_start_on_paper() -> None:
    """Paper validates the ADR-0044 §9 bound and writes nothing.

    Leaving the check to the venue was rejected: live would fail with a venue
    error while **paper accepted an impossible leverage silently** and computed
    margin, liquidation price and effective leverage off it. The strategy then
    behaves one way in paper and fails at boot on promotion — precisely the
    paper/live divergence the identical-compute grain exists to prevent. Paper
    validating without writing is not an inconsistency; it is the
    venue-agnostic half of the check running where the venue-specific half
    cannot.

    ``ETH`` sits *exactly* on its cap, so it must clear: the bound is
    ``1 <= leverage <= max_leverage``, inclusive at both ends, and a refusal
    naming ETH would be an off-by-one this assertion catches.
    """
    exchange = PaperExchange(
        bus=InMemoryBus(),
        clock=ManualClock(),
        fill_model=ImmediateFillModel(),
        genesis_collateral=GENESIS,
        account_net=dict,
        instrument_specs={
            "BTC": replace(_BTC_SPEC, max_leverage=40),
            "ETH": replace(_BTC_SPEC, symbol="ETH", max_leverage=25),
        },
        leverage={
            "BTC": LeverageSpec(mode="cross", leverage=50),
            "ETH": LeverageSpec(leverage=25),
        },
    )

    with pytest.raises(LeverageOutOfBounds) as refusal:
        asyncio.run(exchange.start())

    assert "BTC" in str(refusal.value)
    assert "40" in str(refusal.value)
    assert "ETH" not in str(refusal.value)
