"""``PaperExchange`` + ``ImmediateFillModel`` (ADR-0012 / ADR-0027).

The paper exchange caches the latest tick per symbol and fills a MARKET order on
receipt against that tick's price, emitting a raw ``FillReport`` on the bus (the
exchange owns no saga — ADR-0015). The default fill model is deterministic,
optimistic, zero-slippage, full-fill: no RNG at all.
"""

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest
from ledgers import GENESIS
from seam_claims import assert_every_member_is_claimed

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange, PaperExchangeConfig
from tickwright.domain import (
    AggressorSide,
    Event,
    Exchange,
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
        bus=bus, clock=clock, fill_model=ImmediateFillModel(), genesis_collateral=GENESIS
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
        bus=bus, clock=clock, fill_model=ImmediateFillModel(), genesis_collateral=GENESIS
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
        genesis_collateral=GENESIS,
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
    )

    assert exchange.account_spec().account_id == "paper-default"
    assert exchange.account_spec().account_id == f"paper-{PaperExchangeConfig().account_label}"


def test_the_lifecycle_pair_reaches_nothing_because_the_one_link_is_constructed() -> None:
    """The two verbs the runner drives (ADR-0024 step 4 and the reverse
    shutdown). In-process there is nothing to connect: this venue's only link —
    the ``MarketTick`` subscription — is wired at construction, so neither verb
    has anything to do.

    Driven *alone*, with no tick and no order, and watched two ways — the paper
    analogue of the live arm's ``post.requests == []``. Dispatch is
    ``isinstance``-guarded (ADR-0023), so a subscription to ``Event`` itself sees
    **everything** this venue publishes, including a type that does not exist
    yet; and a venue that connected nothing leaves no task running behind it.

    Both are needed for the claim to stay honest as the seam grows. ADR-0037's
    funding generator would be started here and cancelled below: it publishes no
    fill, so a test watching fills would survive its arrival silently, and it
    publishes nothing at all until time advances, so even the catch-all would.
    The task is what gives it away."""
    exchange, bus, _clock, _fills = _harness()
    published: list[Event] = []
    bus.subscribe(Event, lambda event: _record(published, event))
    running: list[asyncio.Task[object]] = []

    async def scenario() -> None:
        await exchange.start()
        running.extend(task for task in asyncio.all_tasks() if task is not asyncio.current_task())
        await exchange.stop()

    asyncio.run(scenario())

    assert published == []
    assert running == []


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
    true while this body releases nothing; pinned before ADR-0037's funding
    generator gives it a task to cancel, which is the first way a second call
    can break. The book survives both, so the second release is no more a reset
    than the first."""
    exchange, bus, clock, fills, statuses = _limit_harness()

    async def scenario() -> VenueOrderView | None:
        clock.advance_to(1_000)
        await bus.publish(_tick("42000"))
        await exchange.place(_limit_order("41000"))  # rests, uncrossed
        await exchange.stop()  # no start() ever ran
        await exchange.stop()  # and again: the faulted pass re-walks the membership
        return await exchange.fetch_order("0xabc")

    view = asyncio.run(scenario())

    assert fills == []
    assert view is not None
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
    "start": "test_the_lifecycle_pair_reaches_nothing_because_the_one_link_is_constructed",
    "stop": "test_the_paper_venue_releases_without_a_start_and_keeps_its_book",
    "place": "test_market_order_fills_at_the_latest_tick_price",
    "cancel": "test_cancel_removes_a_resting_order_and_reports_cancelled",
    "fetch_order": "test_fetch_order_reports_a_resting_limit_as_live",
    "account_spec": "test_the_paper_venue_declares_a_two_segment_account_id_and_its_genesis",
    "instrument_specs": "test_instrument_specs_exposes_the_configured_specs",
}


def test_every_exchange_member_carries_a_claim_in_the_paper_suite() -> None:
    """The completeness gate the ``isinstance`` check above cannot be.

    Both adapters must implement a new seam member for the engine to run at all,
    so conformance passes the moment the member exists — while nothing asserts
    what it *does* on this venue. Four more members are queued (#177, #173,
    #174); each arrives cheaper against a gate that names the omission than
    against a reviewer who has to notice it."""
    assert_every_member_is_claimed(Exchange, _SEAM_CLAIMS, suite=Path(__file__).parent)
