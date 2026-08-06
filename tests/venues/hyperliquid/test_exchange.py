"""``HyperliquidExchange`` venue translation (issue #23): mocked-HTTP tests.

The POST transport is the only fake; signing is real (SDK utilities over a
throwaway, publicly-known test key), so every asserted wire is one the venue
could verify — the recovery check proves the signature binds the exact action
sent. Venue quirk translation lives in the adapter, never the engine
(ADR-0030): MARKET becomes an aggressive IOC limit at
``latest × (1 ± slippage_bound)``, quantized per the ADR-0017 price rule.
"""

import asyncio
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pytest
from eth_account import Account
from hyperliquid.utils.signing import recover_agent_or_user_from_l1_action
from hyperliquid_fakes import FakeExchangeApi, request_type, resting_response
from pydantic import SecretStr
from seam_claims import assert_every_member_is_claimed

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.domain import (
    AggressorSide,
    Exchange,
    ExecutionReport,
    FillReport,
    InstrumentSpec,
    MarketTick,
    OrderState,
    OrderStatusReport,
    OrderType,
    PlaceOrder,
    Side,
    TimeInForce,
    VenueFactUnsupported,
    VenueOrderView,
    VenueReadFailure,
)
from tickwright.observability import NamedEvent
from tickwright.observability.testing import capture_events
from tickwright.venues.hyperliquid import (
    HyperliquidConfig,
    HyperliquidExchange,
    HyperliquidUniverse,
)

# Anvil's account #0 — a publicly-known throwaway key, safe in a test file.
TEST_SIGNING_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
CLOID = "0x" + "ab" * 16

BTC_SPEC = InstrumentSpec(
    symbol="BTC",
    sz_decimals=5,
    max_decimals=6,
    min_notional=Decimal("10"),
    max_sig_figs=5,
)
UNIVERSE = HyperliquidUniverse(specs={"BTC": BTC_SPEC}, asset_indices={"BTC": 3})

_NS_PER_MS = 1_000_000


def make_exchange(
    post: FakeExchangeApi, *, bus: InMemoryBus, clock: ManualClock
) -> HyperliquidExchange:
    config = HyperliquidConfig(
        testnet=True,
        symbols=["BTC"],
        signing_key=SecretStr(TEST_SIGNING_KEY),
        slippage_bound=Decimal("0.05"),
    )
    return HyperliquidExchange(
        config=config,
        bus=bus,
        clock=clock,
        universe=UNIVERSE,
        post=post,
        # ADR-0024's barrier budget, which the boot guards in ``start()`` are
        # bounded by. Only the mode-gate tests in ``test_preflight.py`` spend it.
        startup_timeout_seconds=60.0,
    )


def tick(symbol: str, price: str) -> MarketTick:
    return MarketTick(
        symbol=symbol,
        price=Decimal(price),
        size=Decimal("1"),
        aggressor_side=AggressorSide.BUY,
        trade_id="1",
        seq=0,
        venue_trade_id=True,
        ts_event=1_700_000_000_000 * _NS_PER_MS,
        ts_init=1_700_000_000_000 * _NS_PER_MS,
    )


def market_order(side: Side, quantity: str) -> PlaceOrder:
    return PlaceOrder(
        cloid=CLOID,
        symbol="BTC",
        side=side,
        quantity=Decimal(quantity),
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.IOC,
    )


def test_market_buy_places_an_aggressive_ioc_limit_at_the_bounded_price() -> None:
    async def main() -> tuple[FakeExchangeApi, ManualClock]:
        bus = InMemoryBus()
        clock = ManualClock(start_ns=1_700_000_001_000 * _NS_PER_MS)
        post = FakeExchangeApi({"order": resting_response(oid=77)})
        exchange = make_exchange(post, bus=bus, clock=clock)
        await bus.publish(tick("BTC", "43250.5"))
        await exchange.place(market_order(Side.BUY, "0.5"))
        return post, clock

    post, clock = asyncio.run(main())

    url, payload = post.requests[0]
    assert url == "https://api.hyperliquid-testnet.xyz/exchange"
    action = payload["action"]
    # 43250.5 × 1.05 = 45413.025, quantized to the 5-sig-fig ∧ 1-decimal grid
    # (buy rounds down, so the bound is never exceeded) → 45413. IOC, never a
    # native market type; cloid rides along for reconcile-by-cloid.
    assert action == {
        "type": "order",
        "orders": [
            {
                "a": 3,
                "b": True,
                "p": "45413",
                "s": "0.5",
                "r": False,
                "t": {"limit": {"tif": "Ioc"}},
                "c": CLOID,
            }
        ],
        "grouping": "na",
    }
    # The nonce is the injected clock's ms timestamp (ADR-0005), and the
    # signature recovers to the signing key's address over exactly this action
    # on testnet — a venue-verifiable wire, not just a plausible-looking one.
    assert payload["nonce"] == clock.timestamp_ns() // _NS_PER_MS
    recovered = recover_agent_or_user_from_l1_action(
        action, payload["signature"], None, payload["nonce"], None, False
    )
    assert recovered == Account.from_key(TEST_SIGNING_KEY).address


def test_market_sell_places_an_aggressive_ioc_limit_at_the_bounded_price() -> None:
    async def main() -> FakeExchangeApi:
        bus = InMemoryBus()
        clock = ManualClock(start_ns=1_700_000_001_000 * _NS_PER_MS)
        post = FakeExchangeApi({"order": resting_response(oid=78)})
        exchange = make_exchange(post, bus=bus, clock=clock)
        await bus.publish(tick("BTC", "43250.5"))
        await exchange.place(market_order(Side.SELL, "0.25"))
        return post

    post = asyncio.run(main())

    (_, payload) = post.requests[0]
    (wire,) = payload["action"]["orders"]
    # 43250.5 × 0.95 = 41087.975, quantized to the 5-sig-fig grid — a sell
    # rounds up, so the price never falls below the slippage floor → 41088.
    assert wire["b"] is False
    assert wire["p"] == "41088"
    assert wire["s"] == "0.25"
    assert wire["t"] == {"limit": {"tif": "Ioc"}}


def limit_order(
    side: Side,
    quantity: str,
    price: str,
    *,
    tif: TimeInForce = TimeInForce.GTC,
    post_only: bool = False,
) -> PlaceOrder:
    return PlaceOrder(
        cloid=CLOID,
        symbol="BTC",
        side=side,
        quantity=Decimal(quantity),
        order_type=OrderType.LIMIT,
        time_in_force=tif,
        price=Decimal(price),
        post_only=post_only,
    )


def placed_wire(post: FakeExchangeApi) -> dict:
    (_, payload) = post.requests[0]
    (wire,) = payload["action"]["orders"]
    return wire


def test_post_only_limit_maps_to_alo() -> None:
    async def main() -> FakeExchangeApi:
        bus = InMemoryBus()
        clock = ManualClock()
        post = FakeExchangeApi({"order": resting_response(oid=79)})
        exchange = make_exchange(post, bus=bus, clock=clock)
        await exchange.place(limit_order(Side.BUY, "0.5", "42000", post_only=True))
        return post

    wire = placed_wire(asyncio.run(main()))
    # post_only means maker-only, and ALO is the venue's spelling of exactly
    # that guarantee (ADR-0030). The guard-quantized price passes through.
    assert wire["t"] == {"limit": {"tif": "Alo"}}
    assert wire["p"] == "42000"


def test_limit_passes_through_with_its_own_time_in_force() -> None:
    async def main() -> tuple[FakeExchangeApi, FakeExchangeApi]:
        bus = InMemoryBus()
        clock = ManualClock()
        gtc_post = FakeExchangeApi({"order": resting_response(oid=80)})
        ioc_post = FakeExchangeApi({"order": resting_response(oid=81)})
        await make_exchange(gtc_post, bus=bus, clock=clock).place(
            limit_order(Side.BUY, "0.5", "42000")
        )
        await make_exchange(ioc_post, bus=InMemoryBus(), clock=clock).place(
            limit_order(Side.SELL, "0.5", "42000.5", tif=TimeInForce.IOC)
        )
        return gtc_post, ioc_post

    gtc_post, ioc_post = asyncio.run(main())
    # A LIMIT has no venue quirk: price and TIF pass through untranslated.
    gtc = placed_wire(gtc_post)
    assert gtc["t"] == {"limit": {"tif": "Gtc"}}
    assert gtc["p"] == "42000"
    ioc = placed_wire(ioc_post)
    assert ioc["t"] == {"limit": {"tif": "Ioc"}}
    assert ioc["p"] == "42000.5"
    assert ioc["b"] is False


async def place_and_collect_reports(
    post: FakeExchangeApi, order: PlaceOrder, *, prime_tick: bool = True
) -> list[ExecutionReport]:
    """Drive one placement against canned responses; return the raw venue
    facts the adapter emitted on the bus (its ``Exchange`` contract)."""
    bus = InMemoryBus()
    clock = ManualClock(start_ns=1_700_000_001_000 * _NS_PER_MS)
    exchange = make_exchange(post, bus=bus, clock=clock)
    reports: list[ExecutionReport] = []

    async def collect(report: ExecutionReport) -> None:
        reports.append(report)

    bus.subscribe(ExecutionReport, collect)
    if prime_tick:
        await bus.publish(tick("BTC", "43250.5"))
    await exchange.place(order)
    return reports


def test_a_resting_placement_reports_the_order_live() -> None:
    post = FakeExchangeApi({"order": resting_response(oid=77)})
    reports = asyncio.run(place_and_collect_reports(post, limit_order(Side.BUY, "0.5", "42000")))

    (report,) = reports
    assert isinstance(report, OrderStatusReport)
    assert report.status is OrderState.LIVE
    assert report.cloid == CLOID
    assert report.symbol == "BTC"
    assert report.venue_oid == "77"


def error_response(message: str) -> dict:
    """The venue's per-order placement refusal (still HTTP 200 / status ok)."""
    return {
        "status": "ok",
        "response": {"type": "order", "data": {"statuses": [{"error": message}]}},
    }


def test_a_placement_error_reports_the_order_rejected_with_the_venue_reason() -> None:
    post = FakeExchangeApi({"order": error_response("Order must have minimum value of $10")})
    reports = asyncio.run(place_and_collect_reports(post, limit_order(Side.BUY, "0.0001", "42000")))

    (report,) = reports
    assert isinstance(report, OrderStatusReport)
    # Venue-adjudicated refusal: REJECTED (sent, judged), never DENIED
    # (ADR-0010) — the reason rides along for the OrderRejected.
    assert report.status is OrderState.REJECTED
    assert report.reason == "Order must have minimum value of $10"
    assert report.cloid == CLOID


def filled_response(*, oid: int, total_sz: str, avg_px: str) -> dict:
    """The venue's placement response for an order that filled on arrival."""
    return {
        "status": "ok",
        "response": {
            "type": "order",
            "data": {"statuses": [{"filled": {"totalSz": total_sz, "avgPx": avg_px, "oid": oid}}]},
        },
    }


def fill_entry(
    *,
    oid: int,
    tid: int,
    px: object,
    sz: object,
    time: int = 1_700_000_000_500,
    fee: object = "0.0",
    fee_token: str = "USDC",
    crossed: bool = True,
) -> dict:
    """One venue ``userFills`` entry (the fields the docs pin, ADR-0011).

    ``px``/``sz`` are ``object``: the venue reports both as decimal strings, so
    building a re-typed one is how a contract change gets tested.
    """
    return {
        "coin": "BTC",
        "px": px,
        "sz": sz,
        "side": "B",
        "time": time,
        "startPosition": "0.0",
        "dir": "Open Long",
        "closedPnl": "0.0",
        "hash": "0x" + "00" * 32,
        "oid": oid,
        "crossed": crossed,
        "fee": fee,
        "feeToken": fee_token,
        "tid": tid,
    }


def test_a_filled_placement_fetches_and_emits_the_real_venue_fills() -> None:
    # The placement response carries no trade ids, and inventing one would
    # double-count against reconciliation's {cloid}:fill:{tid} dedup — so the
    # adapter follows up with a fills read and emits the venue's own records,
    # filtered to this order's oid.
    post = FakeExchangeApi(
        {
            "order": filled_response(oid=91, total_sz="0.5", avg_px="43250.0"),
            "userFills": [
                fill_entry(oid=90, tid=555, px="43249.0", sz="1.0"),
                fill_entry(oid=91, tid=556, px="43250.0", sz="0.3"),
                fill_entry(oid=91, tid=557, px="43250.5", sz="0.2"),
            ],
        }
    )
    reports = asyncio.run(place_and_collect_reports(post, market_order(Side.BUY, "0.5")))

    (fills_url, fills_query) = post.requests[1]
    assert fills_url == "https://api.hyperliquid-testnet.xyz/info"
    assert fills_query == {
        "type": "userFills",
        "user": Account.from_key(TEST_SIGNING_KEY).address,
    }
    first, second = reports
    assert isinstance(first, FillReport) and isinstance(second, FillReport)
    assert (first.trade_id, first.price, first.quantity) == (
        "556",
        Decimal("43250.0"),
        Decimal("0.3"),
    )
    assert (second.trade_id, second.price, second.quantity) == (
        "557",
        Decimal("43250.5"),
        Decimal("0.2"),
    )
    assert first.cloid == CLOID
    assert first.ts_event == 1_700_000_000_500 * _NS_PER_MS  # the venue's fill time


def test_a_live_fill_reports_the_fee_the_venue_charged_verbatim() -> None:
    # The venue is the fee's authority on this path, so the adapter reads its
    # number rather than reconstructing one from a tier schedule (ADR-0036): the
    # account's own volume tier, any referral discount and the venue's 6-dp
    # truncation are all already in it, and none of them is knowable from here.
    # The maker/taker flag is baked in for the same reason — the reported figure
    # for a `crossed: false` fill already reflects the maker rate, so nothing
    # downstream needs the bit and it is not carried (ADR-0036).
    post = FakeExchangeApi(
        {
            "order": filled_response(oid=91, total_sz="0.002", avg_px="65239.0"),
            "userFills": [
                fill_entry(oid=91, tid=556, px="65239.0", sz="0.002", fee="0.019571", crossed=False)
            ],
        }
    )

    reports = asyncio.run(place_and_collect_reports(post, market_order(Side.BUY, "0.002")))

    (report,) = reports
    assert isinstance(report, FillReport)
    assert report.fee == Decimal("0.019571")


def test_a_live_fill_settled_in_another_token_escalates_instead_of_freezing() -> None:
    # Money is a bare `Decimal` with USDC left implicit (ADR-0029), so a fee
    # denominated in anything else has no home in the ledger — accruing it would
    # add a number of one currency to a line of another and silently misstate
    # cash. It is refused, but deliberately *not* as the transient failed read
    # every other unreadable body gets: the venue's stored fill row is immutable,
    # so the read fails identically on every later pass, and a `None` here would
    # freeze the reconcile cycle permanently — one HYPE-settled fee stalling
    # every order behind it, forever, on a `RECONCILE_FROZEN` a cycle. A
    # condition no retry can resolve leaves the seam instead (ADR-0036 §4).
    post = FakeExchangeApi(
        {
            "orderStatus": order_status_response(status="filled", oid=91),
            "userFillsByTime": [
                fill_entry(oid=91, tid=556, px="43250.0", sz="0.5", fee="0.02", fee_token="HYPE")
            ],
        }
    )

    with pytest.raises(VenueFactUnsupported, match="HYPE"):
        asyncio.run(fetch_view(post))


def test_a_permanent_refusal_escapes_the_write_guard_that_catches_everything_else() -> None:
    # The write path now answers an unreadable body with a named no-report, and
    # this is the one body it must not answer that way. A fee in a token the
    # ledger cannot hold is permanent, so filing it as retryable would bury it:
    # the placement returns quietly, and every later reconcile pass re-reads the
    # same settled row to the same refusal and freezes on it. The guard catches
    # `UNREADABLE`, and this deliberately is not in it (ADR-0048) — nothing here
    # has to remember the distinction for it to hold.
    post = FakeExchangeApi(
        {
            "order": filled_response(oid=91, total_sz="0.5", avg_px="43250.0"),
            "userFills": [
                fill_entry(oid=91, tid=556, px="43250.0", sz="0.5", fee="0.02", fee_token="HYPE")
            ],
        }
    )

    with pytest.raises(VenueFactUnsupported, match="HYPE"):
        asyncio.run(place_and_collect_reports(post, market_order(Side.BUY, "0.5")))


def test_a_filled_placement_whose_fills_read_fails_names_a_fills_failure_not_a_place() -> None:
    # The order filled, but the follow-up fills read dies in transport. The
    # placement itself succeeded, so naming it a *place* failure would mislead
    # triage — name it a fills-read failure, emit nothing, and let
    # reconciliation's fetch_order re-read FILLED and heal the fills (R004).
    #
    # The label is the venue query, `userFills`, which is what the event catalog
    # documents and what the *unreadable* half of this same read always emitted.
    # It used to be `fills` on this half alone: one read naming itself two ways
    # depending on which way it failed, which is the disagreement the shared read
    # exists to end. The R004 distinction is untouched — it is about not saying
    # `place`.
    post = FakeExchangeApi(
        {
            "order": filled_response(oid=91, total_sz="0.5", avg_px="43250.0"),
            "userFills": ConnectionError("connection refused"),
        }
    )

    with capture_events() as events:
        reports = asyncio.run(place_and_collect_reports(post, market_order(Side.BUY, "0.5")))

    assert reports == []
    failed = [e for e in events if e["event"] == NamedEvent.EXCHANGE_REQUEST_FAILED]
    assert failed and failed[0]["request"] == "userFills"


def cancel_success_response() -> dict:
    return {
        "status": "ok",
        "response": {"type": "cancel", "data": {"statuses": ["success"]}},
    }


def test_cancel_sends_a_signed_cancel_by_cloid_and_reports_cancelled() -> None:
    async def main() -> tuple[FakeExchangeApi, list[ExecutionReport]]:
        bus = InMemoryBus()
        clock = ManualClock(start_ns=1_700_000_001_000 * _NS_PER_MS)
        post = FakeExchangeApi(
            {"order": resting_response(oid=77), "cancelByCloid": cancel_success_response()}
        )
        exchange = make_exchange(post, bus=bus, clock=clock)
        reports: list[ExecutionReport] = []

        async def collect(report: ExecutionReport) -> None:
            reports.append(report)

        bus.subscribe(ExecutionReport, collect)
        await exchange.place(limit_order(Side.BUY, "0.5", "42000"))
        await exchange.cancel(CLOID)
        return post, reports

    post, reports = asyncio.run(main())

    (url, payload) = post.requests[1]
    assert url == "https://api.hyperliquid-testnet.xyz/exchange"
    action = payload["action"]
    assert action == {"type": "cancelByCloid", "cancels": [{"asset": 3, "cloid": CLOID}]}
    recovered = recover_agent_or_user_from_l1_action(
        action, payload["signature"], None, payload["nonce"], None, False
    )
    assert recovered == Account.from_key(TEST_SIGNING_KEY).address
    # The venue accepted the cancel: the raw CANCELLED fact goes on the bus.
    live, cancelled = reports
    assert isinstance(cancelled, OrderStatusReport)
    assert cancelled.status is OrderState.CANCELLED
    assert cancelled.cloid == CLOID


def order_status_response(
    *, coin: str = "BTC", status: str = "open", oid: int = 77, cloid: str | None = None
) -> dict:
    """The venue's orderStatus answer for a known order."""
    return {
        "status": "order",
        "order": {
            "order": {
                "coin": coin,
                "side": "B",
                "limitPx": "42000.0",
                "sz": "0.5",
                "oid": oid,
                "timestamp": 1_700_000_000_000,
                "origSz": "0.5",
                "cloid": cloid or CLOID,
            },
            "status": status,
            "statusTimestamp": 1_700_000_000_100,
        },
    }


def test_cancel_of_an_order_placed_before_a_crash_resolves_the_coin_from_venue_truth() -> None:
    # A restart empties the adapter's placed-order memory, but the engine still
    # cancels by cloid (ADR-0026) — so the adapter asks the venue whose order
    # this is (orderStatus) and cancels with the resolved asset index.
    async def main() -> FakeExchangeApi:
        post = FakeExchangeApi(
            {"orderStatus": order_status_response(), "cancelByCloid": cancel_success_response()}
        )
        exchange = make_exchange(post, bus=InMemoryBus(), clock=ManualClock())
        await exchange.cancel(CLOID)
        return post

    post = asyncio.run(main())

    (status_url, status_query) = post.requests[0]
    assert status_url == "https://api.hyperliquid-testnet.xyz/info"
    assert status_query == {
        "type": "orderStatus",
        "user": Account.from_key(TEST_SIGNING_KEY).address,
        "oid": CLOID,
    }
    (_, cancel_payload) = post.requests[1]
    assert cancel_payload["action"] == {
        "type": "cancelByCloid",
        "cancels": [{"asset": 3, "cloid": CLOID}],
    }


def test_cancel_of_a_cloid_the_venue_never_saw_is_a_benign_no_op() -> None:
    async def main() -> FakeExchangeApi:
        post = FakeExchangeApi({"orderStatus": {"status": "unknownOid"}})
        exchange = make_exchange(post, bus=InMemoryBus(), clock=ManualClock())
        await exchange.cancel(CLOID)
        return post

    post = asyncio.run(main())
    # One venue read, no cancel action: nothing to cancel, nothing to report
    # (ADR-0026) — the venue positively has no record of this cloid.
    assert len(post.requests) == 1


async def fetch_view(post: FakeExchangeApi) -> VenueOrderView | VenueReadFailure:
    exchange = make_exchange(post, bus=InMemoryBus(), clock=ManualClock())
    return await exchange.fetch_order(CLOID)


def test_fetch_order_bundles_the_venue_status_and_fills_into_one_view() -> None:
    post = FakeExchangeApi(
        {
            "orderStatus": order_status_response(status="filled", oid=91),
            "userFillsByTime": [
                fill_entry(oid=90, tid=555, px="43249.0", sz="1.0"),
                fill_entry(oid=91, tid=556, px="43250.0", sz="0.5"),
            ],
        }
    )
    view = asyncio.run(fetch_view(post))

    assert isinstance(view, VenueOrderView)
    assert view.has_record
    assert view.status is not None
    assert view.status.status is OrderState.FILLED
    assert view.status.cloid == CLOID
    assert view.status.symbol == "BTC"
    assert view.status.venue_oid == "91"
    # The ADR-0011 cross-check in one read: this order's fills, by its oid.
    (fill,) = view.fills
    assert (fill.trade_id, fill.price, fill.quantity) == ("556", Decimal("43250.0"), Decimal("0.5"))
    # The fills read is bounded at the order's own venue placement time, so its
    # fills sit at the front of the window and the venue's page cap can never
    # push them out (the whole-history read this replaces could — R001).
    (_, fills_query) = post.requests[1]
    assert fills_query == {
        "type": "userFillsByTime",
        "user": Account.from_key(TEST_SIGNING_KEY).address,
        "startTime": 1_700_000_000_000,
    }


def test_fetch_order_returns_an_empty_view_when_the_venue_has_no_record() -> None:
    # unknownOid is a *successful* read: positive proof the order never landed
    # (the ADR-0008 resend gate), categorically different from a failed read.
    view = asyncio.run(fetch_view(FakeExchangeApi({"orderStatus": {"status": "unknownOid"}})))

    assert isinstance(view, VenueOrderView)
    assert not view.has_record
    assert view.status is None
    assert view.fills == ()


def test_fetch_order_reports_a_failed_send_when_the_read_itself_fails() -> None:
    # The connectivity guard (ADR-0011 inv 1): a timeout or transport error is
    # a failure — never an empty view, which would read as "no record" and let
    # recovery resend into an outage.
    #
    # `SEND_FAILED` specifically, and that is not decoration: no body arrived,
    # so the venue may be unreachable, and it is the one failure on which the
    # reconciler is entitled to stop reading the rest of its worklist rather
    # than pay a request timeout per order to learn the same thing (ADR-0049).
    #
    # And *named*, on the half where the read died. This freeze used to go out
    # silently — `fetch_order` swallowed the OSError with a bare `return None` —
    # which is the same quiet outage the unreadable-body branches beside it were
    # fixed for: an operator holding a frozen cycle and no event cannot tell a
    # dead venue from a healthy quiet one. The `request` label is what says which
    # half of the two-part read (ADR-0011's cross-check) failed, so it is pinned
    # per case rather than asserted as "something was named".
    for failure in (TimeoutError("venue timed out"), ConnectionError("connection refused")):
        with capture_events() as events:
            view = asyncio.run(fetch_view(FakeExchangeApi({"orderStatus": failure})))
        assert view is VenueReadFailure.SEND_FAILED
        failures = [e for e in events if e["event"] == NamedEvent.EXCHANGE_REQUEST_FAILED]
        assert failures and failures[0]["request"] == "orderStatus", failure

    # A failure on the *fills* half of the read poisons the whole view too.
    with capture_events() as events:
        view = asyncio.run(
            fetch_view(
                FakeExchangeApi(
                    {
                        "orderStatus": order_status_response(),
                        "userFillsByTime": ConnectionError("reset"),
                    }
                )
            )
        )
    assert view is VenueReadFailure.SEND_FAILED
    failures = [e for e in events if e["event"] == NamedEvent.EXCHANGE_REQUEST_FAILED]
    assert failures and failures[0]["request"] == "userFills"


def test_fetch_order_names_an_order_status_body_it_cannot_parse() -> None:
    # An orderStatus body outside the venue's two documented shapes — an order
    # record, or the positive `unknownOid` — is a failed read, and the adapter
    # already froze the reconciler on it. What it did not do is *say so*: the
    # freeze went out with no named event behind it, which is the quiet outage
    # inv 1 exists to prevent, and left a venue contract change looking exactly
    # like an ordinary quiet cycle.
    post = FakeExchangeApi({"orderStatus": {"status": "someNewEnvelope"}})

    with capture_events() as events:
        view = asyncio.run(fetch_view(post))

    # `UNREADABLE_BODY`, not the failed send beside it: the venue answered, and
    # answered promptly. Nothing about this body says anything about the next
    # order's, so the reconciler may skip this one and keep reading (ADR-0049).
    assert view is VenueReadFailure.UNREADABLE_BODY
    failures = [e for e in events if e["event"] == NamedEvent.EXCHANGE_REQUEST_FAILED]
    assert failures and failures[0]["request"] == "orderStatus"


def test_fetch_order_freezes_on_a_fills_body_it_cannot_parse() -> None:
    # A 200-OK fills body of the wrong shape is a failed read, not "no fills":
    # it must freeze the reconciler (None), the same as an unparseable
    # orderStatus — never a partial view that reads as an empty book (ADR-0011
    # inv 1) — and name the shape change for triage.
    post = FakeExchangeApi(
        {"orderStatus": order_status_response(status="open"), "userFillsByTime": {"unexpected": 1}}
    )

    with capture_events() as events:
        view = asyncio.run(fetch_view(post))

    assert view is VenueReadFailure.UNREADABLE_BODY
    assert any(e["event"] == NamedEvent.EXCHANGE_REQUEST_FAILED for e in events)


@pytest.mark.parametrize("figure", ["NaN", "Infinity", "-Infinity"])
def test_fetch_order_freezes_on_a_non_finite_fill_figure(figure: str) -> None:
    # `Decimal("NaN")`/`Decimal("Infinity")` construct cleanly, so a non-finite
    # `sz`/`px` is the one unreadable fill figure that raises nothing on the way
    # in. It must freeze like any other unparseable body (ADR-0011 inv 1): a NaN
    # quantity poisons cum_qty by arithmetic and leaves its equality cross-check
    # permanently disagreeing, and it is durable once written — the store
    # round-trips "NaN" back into a Decimal("NaN") on recovery.
    for entry in (
        fill_entry(oid=91, tid=556, px=figure, sz="0.5"),
        fill_entry(oid=91, tid=556, px="43250.0", sz=figure),
    ):
        post = FakeExchangeApi(
            {
                "orderStatus": order_status_response(status="filled", oid=91),
                "userFillsByTime": [entry],
            }
        )

        with capture_events() as events:
            view = asyncio.run(fetch_view(post))

        assert view is VenueReadFailure.UNREADABLE_BODY
        assert any(e["event"] == NamedEvent.EXCHANGE_REQUEST_FAILED for e in events)


@pytest.mark.parametrize("figure", [18.435, 93, 1e30, 43250.123456789012345])
def test_fetch_order_freezes_on_a_re_typed_fill_figure(figure: object) -> None:
    # The venue reports `px`/`sz` as decimal strings — first-party `userFills`
    # and the pinned SDK `Fill` TypedDict agree — so a JSON *number* is the venue
    # changing its contract, and inv 1 says a body we cannot read is a failed
    # read, never a partial truth. It cannot be coerced through either, and the
    # loss is not in our parse — `Decimal(str(x))` round-trips `0.002` exactly.
    # It is in `json.loads`: a JSON number is a `float` before this reader sees
    # it, so a reported `43250.123456789012345` arrives as `43250.12345678901`
    # and the reported scale is gone too. No longer exact (ADR-0029), and durable
    # the moment `_records.py` round-trips the fill it computed.
    for entry in (
        fill_entry(oid=91, tid=556, px=figure, sz="0.5"),
        fill_entry(oid=91, tid=556, px="43250.0", sz=figure),
    ):
        post = FakeExchangeApi(
            {
                "orderStatus": order_status_response(status="filled", oid=91),
                "userFillsByTime": [entry],
            }
        )

        with capture_events() as events:
            view = asyncio.run(fetch_view(post))

        assert view is VenueReadFailure.UNREADABLE_BODY
        assert any(e["event"] == NamedEvent.EXCHANGE_REQUEST_FAILED for e in events)


@pytest.mark.parametrize(
    ("venue_status", "state"),
    [
        ("open", OrderState.LIVE),
        ("canceled", OrderState.CANCELLED),
        ("marginCanceled", OrderState.CANCELLED),
        ("scheduledCancel", OrderState.CANCELLED),
        ("minTradeNtlRejected", OrderState.REJECTED),
        ("badAloPxRejected", OrderState.REJECTED),
    ],
)
def test_fetch_order_maps_the_venue_status_taxonomy_by_suffix(
    venue_status: str, state: OrderState
) -> None:
    post = FakeExchangeApi(
        {"orderStatus": order_status_response(status=venue_status), "userFillsByTime": []}
    )
    view = asyncio.run(fetch_view(post))

    assert isinstance(view, VenueOrderView) and view.status is not None
    assert view.status.status is state


def test_fetch_order_treats_a_status_it_cannot_map_as_a_named_failed_read() -> None:
    # A venue status outside the known taxonomy (say, a trigger state v1 never
    # places) must freeze the reconciler, not get misclassified as venue truth —
    # and must name which status did it. This body parses perfectly; the freeze
    # comes from a *value* the saga vocabulary has no term for, so the venue
    # string is the whole of the triage, and an unnamed freeze leaves an operator
    # with a stalled cycle and nowhere to look.
    with capture_events() as events:
        view = asyncio.run(
            fetch_view(FakeExchangeApi({"orderStatus": order_status_response(status="triggered")}))
        )

    # The unreadable body, not the failed send: this is issue #236's own case at
    # the venue grain, and the venue's stored status string does not change, so
    # every later pass reads it identically. Answering it as an outage is what
    # let one order freeze every order behind it, forever.
    assert view is VenueReadFailure.UNREADABLE_BODY
    failures = [e for e in events if e["event"] == NamedEvent.EXCHANGE_REQUEST_FAILED]
    assert failures and failures[0]["request"] == "orderStatus"
    assert "triggered" in str(failures[0]["error"])


def test_a_transport_failure_on_place_emits_no_report_and_does_not_raise() -> None:
    # The send window's truth is unknown — the order may or may not have
    # landed — so the adapter reports nothing (no fact to report) and lets
    # reconcile-by-cloid resolve the in-flight order (ADR-0008 rule 2). It
    # names the failure for triage instead of faulting the engine.
    post = FakeExchangeApi({"order": ConnectionError("connection refused")})

    with capture_events() as events:
        reports = asyncio.run(
            place_and_collect_reports(post, limit_order(Side.BUY, "0.5", "42000"))
        )

    assert reports == []
    assert any(e["event"] == NamedEvent.EXCHANGE_REQUEST_FAILED for e in events)


def test_a_resting_status_the_adapter_cannot_read_names_it_instead_of_faulting() -> None:
    # The write path carried the read path's hole. `status["resting"]["oid"]`
    # sat inside a `try` catching only `OSError`, so a 200-OK body of the wrong
    # shape raised a `KeyError` straight out of `place` — and nothing downstream
    # catches it: `execution.py` awaits the send unguarded, so it reaches the
    # runner's TaskGroup and takes the whole engine to FAULTED and exit 1 over
    # one unreadable field. An unreadable body is a failed read wherever it is
    # read (inv 1); on the write path that verdict is no report and
    # reconcile-by-cloid as the backstop, exactly as a dead transport gets.
    post = FakeExchangeApi(
        {
            "order": {
                "status": "ok",
                "response": {"type": "order", "data": {"statuses": [{"resting": {}}]}},
            }
        }
    )

    with capture_events() as events:
        reports = asyncio.run(
            place_and_collect_reports(post, limit_order(Side.BUY, "0.5", "42000"))
        )

    assert reports == []
    failures = [e for e in events if e["event"] == NamedEvent.EXCHANGE_REQUEST_FAILED]
    assert failures and failures[0]["request"] == "place"


@pytest.mark.parametrize(
    "filled",
    [
        {"totalSz": "0.5", "avgPx": "43250.0"},  # no oid to fetch the fills by
        {"totalSz": "0.5", "avgPx": "43250.0", "oid": "n/a"},  # an oid that is not one
    ],
)
def test_a_filled_status_whose_oid_is_unreadable_names_it_instead_of_faulting(
    filled: dict,
) -> None:
    # The `filled` branch's `int(status["filled"]["oid"])` is the second
    # dereference the OSError-only guard left exposed, and it is the worse of
    # the two: this order *executed*, so faulting here kills the engine holding
    # a position it has not recorded. No report is still the right answer — the
    # placement response carries no trade ids, so there is nothing to emit
    # without the fills read this oid is the key to — but the engine has to
    # survive to let reconciliation heal the fills (ADR-0011).
    post = FakeExchangeApi(
        {
            "order": {
                "status": "ok",
                "response": {"type": "order", "data": {"statuses": [{"filled": filled}]}},
            }
        }
    )

    with capture_events() as events:
        reports = asyncio.run(place_and_collect_reports(post, market_order(Side.BUY, "0.5")))

    assert reports == []
    failures = [e for e in events if e["event"] == NamedEvent.EXCHANGE_REQUEST_FAILED]
    assert failures and failures[0]["request"] == "place"


@pytest.mark.parametrize(
    "body",
    [
        # An envelope that is neither the documented `ok` nor the documented
        # `err`: `_action_outcome` used to fail fast on this by raising.
        {"status": "ok", "response": {"type": "order", "data": {}}},
        # The envelope parses, but the adjudication inside it is not one of the
        # three the adapter knows (`resting`, `error`, `filled`).
        {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"queued": {}}]}}},
    ],
)
def test_a_placement_adjudication_the_adapter_cannot_read_is_named_not_silent(body: dict) -> None:
    # Two failure modes that were opposites and are now one answer. The
    # unrecognized *envelope* raised a ValueError out of `place` and faulted the
    # engine — deliberately, per `_action_outcome`, but an unreadable body is a
    # failed read wherever it is read (inv 1), and the write path has the same
    # backstop the read path does. The unrecognized *status entry* did the
    # reverse and was worse: it fell through every branch and returned, emitting
    # nothing at all, so a venue that started adjudicating a fourth way would
    # leave orders silently unreported with no trace anywhere.
    post = FakeExchangeApi({"order": body})

    with capture_events() as events:
        reports = asyncio.run(
            place_and_collect_reports(post, limit_order(Side.BUY, "0.5", "42000"))
        )

    assert reports == []
    failures = [e for e in events if e["event"] == NamedEvent.EXCHANGE_REQUEST_FAILED]
    assert failures and failures[0]["request"] == "place"


def test_an_unreadable_placement_body_is_named_with_the_body_it_could_not_read() -> None:
    # The read path quotes what it choked on — key set first, since that is what
    # identifies a venue contract change — and the write path must not be the
    # one place that names a failed read without saying what failed. An
    # unreadable adjudication repeats on every placement for as long as the
    # contract stays broken, so an operator holding only `ValueError()` has
    # nothing to act on.
    post = FakeExchangeApi(
        {"order": {"status": "ok", "response": {"type": "order", "data": {"statuses": [{}]}}}}
    )

    with capture_events() as events:
        asyncio.run(place_and_collect_reports(post, limit_order(Side.BUY, "0.5", "42000")))

    (failed,) = [e for e in events if e["event"] == NamedEvent.EXCHANGE_REQUEST_FAILED]
    assert "keys=['status', 'response']" in str(failed["error"])


class _HandlerFailure(ValueError):
    """A bug anywhere downstream of a published venue fact.

    Deliberately a ``ValueError``: an engine bug does not get to pick an
    exception type outside the venue's ``UNREADABLE`` vocabulary, and one that
    lands inside it is the whole point — a stand-in that raised something else
    would pass against a guard that swallows the real thing.
    """


@pytest.mark.parametrize(
    "raised",
    [
        ValueError("a saga bug"),
        KeyError("a missing key"),
        TypeError("a bad type"),
        InvalidOperation("a decimal that blew up"),
    ],
    ids=["ValueError", "KeyError", "TypeError", "InvalidOperation"],
)
def test_a_handler_failure_on_the_placement_publish_is_not_an_unreadable_body(
    raised: Exception,
) -> None:
    """A subscriber's exception is an engine fault, never a venue read failure.

    The write guard catches ``UNREADABLE`` around a coroutine that both *reads*
    the venue's adjudication and *publishes* it — and ``InMemoryBus.publish``
    dispatches subscribers inline and re-raises, so the guard spans the whole
    cascade the report sets off: the saga, the checkpoint, the portfolio fold.
    Every member of ``UNREADABLE`` is a shape an engine bug takes too
    (``InvalidOperation`` out of a ``Decimal`` comparison being the likeliest),
    so each was caught by the *venue adapter*, named against a response that had
    parsed cleanly, and survived — flatly against ``runner.py``'s guarantee that
    a raw handler's exception reaches the ``TaskGroup`` and faults the engine
    (ADR-0024).

    ``read`` splits its send from a **pure** ``normalize`` for exactly this
    reason (ADR-0048 §4). The write path took the vocabulary without the split.
    """

    async def main() -> None:
        bus = InMemoryBus()

        async def blows_up(report: ExecutionReport) -> None:
            raise raised

        bus.subscribe(ExecutionReport, blows_up)
        exchange = make_exchange(
            FakeExchangeApi({"order": resting_response(oid=77)}), bus=bus, clock=ManualClock()
        )
        await exchange.place(limit_order(Side.BUY, "0.5", "42000"))

    with capture_events() as events:
        with pytest.raises(type(raised)):
            asyncio.run(main())

    # And it is not *also* mislabelled on the way out: naming a clean body as
    # unreadable would point triage at the venue for a bug in this process.
    assert not [e for e in events if e["event"] == NamedEvent.EXCHANGE_REQUEST_FAILED]


def test_a_handler_failure_on_the_cancel_publish_is_not_an_unreadable_body() -> None:
    # The same hole on the other write verb, guarded by the same tuple.
    async def main() -> None:
        bus = InMemoryBus()

        async def blows_up(report: OrderStatusReport) -> None:
            if report.status is OrderState.CANCELLED:
                raise _HandlerFailure("downstream of the cancel report")

        bus.subscribe(OrderStatusReport, blows_up)
        exchange = make_exchange(
            FakeExchangeApi(
                {"order": resting_response(oid=77), "cancelByCloid": cancel_success_response()}
            ),
            bus=bus,
            clock=ManualClock(),
        )
        await exchange.place(limit_order(Side.BUY, "0.5", "42000"))
        await exchange.cancel(CLOID)

    with capture_events() as events:
        with pytest.raises(_HandlerFailure):
            asyncio.run(main())

    assert not [e for e in events if e["event"] == NamedEvent.EXCHANGE_REQUEST_FAILED]


def test_a_transport_failure_on_cancel_emits_no_report_and_does_not_raise() -> None:
    async def main() -> None:
        post = FakeExchangeApi(
            {"order": resting_response(oid=77), "cancelByCloid": TimeoutError("venue timed out")}
        )
        exchange = make_exchange(post, bus=InMemoryBus(), clock=ManualClock())
        await exchange.place(limit_order(Side.BUY, "0.5", "42000"))
        await exchange.cancel(CLOID)

    with capture_events() as events:
        asyncio.run(main())

    # The cancel_requested marker is already durable (ADR-0026); reconciliation
    # resolves an ack-lost cancel, so the adapter only names the failure.
    assert any(e["event"] == NamedEvent.EXCHANGE_REQUEST_FAILED for e in events)


def action_error_response(message: str) -> dict:
    """The venue's action-level refusal envelope: the whole order/cancel action
    was rejected before adjudication (bad nonce/signature, an action rate-limit),
    at HTTP 200 — distinct from a per-order ``error`` status inside ``statuses``."""
    return {"status": "err", "response": message}


def test_a_top_level_action_error_on_place_emits_no_report_and_names_it() -> None:
    # A top-level `err` envelope means the order never entered the book (the send
    # was refused, not adjudicated) — so the adapter emits no terminal REJECTED
    # (which would wrongly kill a resendable order on a transient nonce/rate
    # error), names the refusal, and lets reconcile-by-cloid resolve. It must not
    # fault the engine by raising an "unrecognized response" ValueError (R001).
    post = FakeExchangeApi({"order": action_error_response("Invalid nonce")})

    with capture_events() as events:
        reports = asyncio.run(
            place_and_collect_reports(post, limit_order(Side.BUY, "0.5", "42000"))
        )

    assert reports == []
    rejected = [e for e in events if e["event"] == NamedEvent.EXCHANGE_ACTION_REJECTED]
    assert rejected and rejected[0]["reason"] == "Invalid nonce"


def test_a_top_level_action_error_on_cancel_emits_no_report_and_names_it() -> None:
    # A refused cancel action (bad nonce/signature/rate-limit) is a benign named
    # no-op: the cancel_requested marker is already durable (ADR-0026), so
    # reconciliation resolves it. It must not raise — no engine fault (R001).
    async def main() -> tuple[list[ExecutionReport], list[str]]:
        bus = InMemoryBus()
        post = FakeExchangeApi(
            {
                "order": resting_response(oid=77),
                "cancelByCloid": action_error_response("Invalid nonce"),
            }
        )
        exchange = make_exchange(post, bus=bus, clock=ManualClock())
        reports: list[ExecutionReport] = []

        async def collect(report: ExecutionReport) -> None:
            reports.append(report)

        bus.subscribe(ExecutionReport, collect)
        await exchange.place(limit_order(Side.BUY, "0.5", "42000"))
        with capture_events() as events:
            await exchange.cancel(CLOID)
        reasons = [
            str(e["reason"]) for e in events if e["event"] == NamedEvent.EXCHANGE_ACTION_REJECTED
        ]
        return reports, reasons

    reports, reasons = asyncio.run(main())
    # Only the LIVE from placement — the refused cancel adds no CANCELLED.
    (live,) = reports
    assert isinstance(live, OrderStatusReport) and live.status is OrderState.LIVE
    assert reasons == ["Invalid nonce"]


def test_a_cancel_adjudication_the_adapter_cannot_read_is_a_named_no_op() -> None:
    # The same hole on the cancel verb, where faulting costs the most for the
    # least: the durable `cancel_requested` marker is written before the send
    # (ADR-0026), so an unanswered cancel is already reconciliation's to resolve.
    # Killing the engine over the *shape* of the answer discards a run that was
    # covered either way.
    async def main() -> tuple[list[ExecutionReport], list[str]]:
        bus = InMemoryBus()
        post = FakeExchangeApi(
            {
                "order": resting_response(oid=77),
                "cancelByCloid": {"status": "ok", "response": {"type": "cancel", "data": {}}},
            }
        )
        exchange = make_exchange(post, bus=bus, clock=ManualClock())
        reports: list[ExecutionReport] = []

        async def collect(report: ExecutionReport) -> None:
            reports.append(report)

        bus.subscribe(ExecutionReport, collect)
        await exchange.place(limit_order(Side.BUY, "0.5", "42000"))
        with capture_events() as events:
            await exchange.cancel(CLOID)
        failed = [
            str(e["request"]) for e in events if e["event"] == NamedEvent.EXCHANGE_REQUEST_FAILED
        ]
        return reports, failed

    reports, failed = asyncio.run(main())

    # Only the LIVE from placement: an unreadable adjudication proves nothing
    # about the cancel, so it must not manufacture a CANCELLED either.
    (live,) = reports
    assert isinstance(live, OrderStatusReport) and live.status is OrderState.LIVE
    assert failed == ["cancel"]


def test_a_per_cancel_error_status_is_a_silent_benign_no_op() -> None:
    # The one adjudication branch on either write verb that is *correctly*
    # silent, pinned so it stays distinguishable from the one that was silent by
    # accident. A per-cancel error means the order is already gone — filled,
    # cancelled, or never landed — which is positive venue truth, not an
    # unreadable body: nothing to name, and nothing to emit, since the venue's
    # real state arrives as its own report or through reconciliation (ADR-0026).
    #
    # Its twin on `place` fell through every branch and reported nothing at all,
    # which read identically from the outside and was a defect (ADR-0048 §4).
    # Only a test that fixes which of the two this is keeps them apart.
    async def main() -> tuple[list[ExecutionReport], list[str]]:
        bus = InMemoryBus()
        post = FakeExchangeApi(
            {
                "order": resting_response(oid=77),
                "cancelByCloid": {
                    "status": "ok",
                    "response": {
                        "type": "cancel",
                        "data": {"statuses": [{"error": "Order was never placed"}]},
                    },
                },
            }
        )
        exchange = make_exchange(post, bus=bus, clock=ManualClock())
        reports: list[ExecutionReport] = []

        async def collect(report: ExecutionReport) -> None:
            reports.append(report)

        bus.subscribe(ExecutionReport, collect)
        await exchange.place(limit_order(Side.BUY, "0.5", "42000"))
        with capture_events() as events:
            await exchange.cancel(CLOID)
        return reports, [str(e["event"]) for e in events]

    reports, named = asyncio.run(main())

    # No CANCELLED — the adapter never adjudicates a cancel the venue refused.
    (live,) = reports
    assert isinstance(live, OrderStatusReport) and live.status is OrderState.LIVE
    # And silent: neither a failed read nor a rejected action, because it is
    # neither. A name here would page an operator for the ordinary race between
    # a cancel and a fill.
    assert named == []


def test_a_cancel_status_outside_the_venue_vocabulary_is_a_failed_read_not_already_gone() -> None:
    # The boundary of the silence above. `ALREADY_GONE` is a claim — the venue
    # says this order is filled, cancelled, or never landed — and the venue makes
    # it one way: a per-cancel `{"error": ...}`. Reading it as "anything that is
    # not the string `success`" would fold a status the venue has never sent into
    # a positive verdict, so a cancel-side contract change would land as the
    # ordinary cancel/fill race and go out silent (ADR-0048 §4).
    #
    # That is the exact defect the place verb was fixed for, and the argument in
    # `_placement_adjudication` is the same one here: a venue that started
    # adjudicating a fourth way would leave orders unreported with nothing
    # recording that it had. An unreadable body is the honest verdict, and the
    # backstop is unchanged — the durable `cancel_requested` marker (ADR-0026)
    # leaves the order to reconciliation either way.
    async def main() -> tuple[list[ExecutionReport], list[str]]:
        bus = InMemoryBus()
        post = FakeExchangeApi(
            {
                "order": resting_response(oid=77),
                "cancelByCloid": {
                    "status": "ok",
                    "response": {"type": "cancel", "data": {"statuses": ["queued"]}},
                },
            }
        )
        exchange = make_exchange(post, bus=bus, clock=ManualClock())
        reports: list[ExecutionReport] = []

        async def collect(report: ExecutionReport) -> None:
            reports.append(report)

        bus.subscribe(ExecutionReport, collect)
        await exchange.place(limit_order(Side.BUY, "0.5", "42000"))
        with capture_events() as events:
            await exchange.cancel(CLOID)
        failed = [
            str(e["request"]) for e in events if e["event"] == NamedEvent.EXCHANGE_REQUEST_FAILED
        ]
        return reports, failed

    reports, failed = asyncio.run(main())

    # Only the LIVE from placement: a status we cannot read proves nothing about
    # the cancel, so it manufactures no CANCELLED — the same verdict as a body
    # the adapter cannot parse at all, because that is what this is.
    (live,) = reports
    assert isinstance(live, OrderStatusReport) and live.status is OrderState.LIVE
    # Named, unlike its already-gone neighbour: the operator's only signal that
    # the venue's cancel vocabulary moved. The status itself rides along, since
    # on a body that parsed cleanly the unrecognized value is the whole triage.
    assert failed == ["cancel"]


def test_placements_within_one_millisecond_get_strictly_increasing_nonces() -> None:
    # Hyperliquid requires per-address nonces to be strictly increasing; the ms
    # truncation of the wall clock would collide on two sends inside one
    # millisecond, so the adapter advances a monotonic floor (R002). ManualClock
    # never ticks, so both sends read the same ms — the second must still exceed
    # the first.
    async def main() -> FakeExchangeApi:
        post = FakeExchangeApi({"order": resting_response(oid=77)})
        exchange = make_exchange(
            post, bus=InMemoryBus(), clock=ManualClock(start_ns=42 * _NS_PER_MS)
        )
        await exchange.place(limit_order(Side.BUY, "0.5", "42000"))
        await exchange.place(limit_order(Side.SELL, "0.5", "42000"))
        return post

    post = asyncio.run(main())
    first_nonce = post.requests[0][1]["nonce"]
    second_nonce = post.requests[1][1]["nonce"]
    assert first_nonce == 42  # the clock's ms, unchanged
    assert second_nonce > first_nonce


def test_a_terminal_fetch_prunes_the_placed_order_so_the_cache_stays_bounded() -> None:
    # _placed lets a cancel resolve a still-open order's coin without a venue
    # read. Once fetch_order sees the order terminate, that memory is dead
    # weight — pruning it keeps the cache bounded by open orders, not by every
    # order the process ever placed (R003). Observable: a later cancel of the
    # pruned cloid falls back to an orderStatus read instead of the cache.
    async def main() -> FakeExchangeApi:
        post = FakeExchangeApi(
            {
                "order": resting_response(oid=77),
                "orderStatus": order_status_response(status="filled"),
                "userFillsByTime": [],
                "cancelByCloid": cancel_success_response(),
            }
        )
        exchange = make_exchange(post, bus=InMemoryBus(), clock=ManualClock())
        await exchange.place(limit_order(Side.BUY, "0.5", "42000"))
        await exchange.fetch_order(CLOID)  # sees FILLED → prunes _placed[CLOID]
        await exchange.cancel(CLOID)
        return post

    post = asyncio.run(main())

    # Two orderStatus reads: the fetch, then the cancel's fallback — which only
    # happens because the terminal fetch pruned the placed-order memory.
    reads = [query for (_, query) in post.requests if query.get("type") == "orderStatus"]
    assert len(reads) == 2


def test_the_venue_link_is_released_without_a_start_having_run() -> None:
    """The faulted teardown walks the same ordered membership as the graceful
    one, so ``stop()`` is reached after a ``start()`` that refused — and after
    one that never ran, an earlier step having faulted first. Releasing an
    adapter that never connected must not reach the venue, and must not raise:
    a break here would be recorded as a stop-hook failure and would cost the
    steps behind it (ADR-0020/0024).

    Driven **twice in the one shutdown**, which is the other way to read the
    same membership: a graceful step that raises *behind* the venue release
    faults the run and the best-effort pass re-walks it from the top, so the
    second call must be a no-op rather than a failure (the runner's half is
    ``test_a_graceful_teardown_that_breaks_releases_the_venue_a_second_time``,
    asserted there on a double and here on the adapter). The fake still routes
    nothing, so neither release may reach the venue."""
    post = FakeExchangeApi({})
    exchange = make_exchange(post, bus=InMemoryBus(), clock=ManualClock())

    async def scenario() -> None:
        await exchange.stop()  # no start() ever ran
        await exchange.stop()  # and again: the faulted pass re-walks the membership

    asyncio.run(scenario())

    assert post.requests == []


def test_the_released_venue_link_still_answers_a_place_and_a_cancel() -> None:
    """Ahead of the bus drain is not ahead of the last caller. ``stop()`` runs
    before the drain, but the drain dispatches the cascade it is waiting on and
    the strategies are still subscribed behind it, so a ``Signal`` can still
    reach the ``ExecutionManager`` and land here as a ``place`` on a released
    adapter. Both order verbs must stay **answerable** across the release —
    never hanging, because the drain is blocked on the cascade they are in.

    ``asyncio.wait_for`` is the "never hanging" half stated as an assertion: an
    unbounded call would hang the suite rather than fail it.

    This adapter holds no connection of its own — every request is scoped to the
    call that makes it — so the release takes nothing away and both verbs reach
    the venue exactly as before. The fake routes only what these two ask for, so
    a released adapter that started sending something *else* fails loudly."""

    async def main() -> tuple[FakeExchangeApi, list[ExecutionReport]]:
        bus = InMemoryBus()
        clock = ManualClock(start_ns=1_700_000_001_000 * _NS_PER_MS)
        post = FakeExchangeApi(
            {
                # The boot gate ``start()`` runs first (ADR-0046 §3), answered
                # with a supported mode so this test reaches the release it is
                # actually about.
                "userAbstraction": "disabled",
                "order": resting_response(oid=77),
                "cancelByCloid": cancel_success_response(),
            }
        )
        exchange = make_exchange(post, bus=bus, clock=clock)
        reports: list[ExecutionReport] = []

        async def collect(report: ExecutionReport) -> None:
            reports.append(report)

        bus.subscribe(ExecutionReport, collect)
        await exchange.start()
        await exchange.stop()
        # Behind the release, inside the drain the runner has not reached yet.
        await asyncio.wait_for(exchange.place(limit_order(Side.BUY, "0.5", "42000")), timeout=5)
        await asyncio.wait_for(exchange.cancel(CLOID), timeout=5)
        return post, reports

    post, reports = asyncio.run(main())

    # The boot read, then both order verbs *behind* the release: what the
    # release took away is nothing, which is the claim.
    assert [request_type(url, payload) for (url, payload) in post.requests] == [
        "userAbstraction",
        "order",
        "cancelByCloid",
    ]
    _live, cancelled = reports
    assert isinstance(cancelled, OrderStatusReport)
    assert cancelled.status is OrderState.CANCELLED
    assert cancelled.cloid == CLOID


def test_the_hyperliquid_venue_satisfies_the_exchange_seam() -> None:
    """Conformance asserted at the adapter, as both bus adapters assert theirs
    (``tests/adapters/bus/``). ``Exchange`` is ``runtime_checkable``, so this is
    a member-presence check: the seam cannot grow a member that leaves this
    adapter behind without failing here. What it cannot see — a member this
    adapter implements but no test asserts — is ``_SEAM_CLAIMS`` below."""
    exchange = make_exchange(FakeExchangeApi({}), bus=InMemoryBus(), clock=ManualClock())

    assert isinstance(exchange, Exchange)


def test_the_venue_hands_out_the_meta_sourced_specs_by_copy() -> None:
    """The specs the composition root wires into the venue-agnostic guard come
    from the venue itself (ADR-0031): the adapter is the one component that
    knows what the meta endpoint said. Handed out as a copy — the universe is
    read once at startup and must not be mutable through a caller that only
    asked to read it, which is what would let a guard and an adapter disagree
    about the same symbol's tick grid."""
    exchange = make_exchange(FakeExchangeApi({}), bus=InMemoryBus(), clock=ManualClock())

    specs = exchange.instrument_specs()

    assert specs == {"BTC": BTC_SPEC}
    assert specs is not UNIVERSE.specs


# Which test claims each ``Exchange`` member for *this* adapter. The gate below
# asserts it against the Protocol itself, so it cannot quietly fall behind. Two
# claims live in the sibling module that owns their subject rather than here —
# the gate reads the whole suite directory, not this file.
_SEAM_CLAIMS = {
    # test_preflight.py — the module that owns what start() now proves.
    "start": "test_a_pooled_account_mode_refuses_to_start_with_the_operator_s_remediation",
    "stop": "test_the_venue_link_is_released_without_a_start_having_run",
    "place": "test_market_buy_places_an_aggressive_ioc_limit_at_the_bounded_price",
    "cancel": "test_cancel_sends_a_signed_cancel_by_cloid_and_reports_cancelled",
    "fetch_order": "test_fetch_order_bundles_the_venue_status_and_fills_into_one_view",
    # test_account.py — the module that owns what clearinghouseState means.
    "fetch_account_state": "test_a_recorded_cross_snapshot_normalizes_to_the_measured_account_figures",
    # test_account.py — the module that owns what qualifies an account id.
    "account_spec": "test_the_account_id_is_qualified_by_venue_network_and_address",
    "instrument_specs": "test_the_venue_hands_out_the_meta_sourced_specs_by_copy",
}


def test_every_exchange_member_carries_a_claim_in_the_hyperliquid_suite() -> None:
    """The completeness gate the ``isinstance`` check above cannot be — the live
    arm of the same gate the paper suite runs.

    It earned its place immediately: ``instrument_specs()`` was on this adapter
    with no test of its own, reached only sideways through ``build_engine``'s
    wiring, and the gate is what named the omission."""
    assert_every_member_is_claimed(Exchange, _SEAM_CLAIMS, suite=Path(__file__).parent)
