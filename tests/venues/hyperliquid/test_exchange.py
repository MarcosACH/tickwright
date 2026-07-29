"""``HyperliquidExchange`` venue translation (issue #23): mocked-HTTP tests.

The POST transport is the only fake; signing is real (SDK utilities over a
throwaway, publicly-known test key), so every asserted wire is one the venue
could verify — the recovery check proves the signature binds the exact action
sent. Venue quirk translation lives in the adapter, never the engine
(ADR-0030): MARKET becomes an aggressive IOC limit at
``latest × (1 ± slippage_bound)``, quantized per the ADR-0017 price rule.
"""

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest
from eth_account import Account
from hyperliquid.utils.signing import recover_agent_or_user_from_l1_action
from hyperliquid_fakes import FakeExchangeApi, resting_response
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
    VenueOrderView,
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
    return HyperliquidExchange(config=config, bus=bus, clock=clock, universe=UNIVERSE, post=post)


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


def fill_entry(*, oid: int, tid: int, px: str, sz: str, time: int = 1_700_000_000_500) -> dict:
    """One venue ``userFills`` entry (the fields the docs pin, ADR-0011)."""
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
        "crossed": True,
        "fee": "0.0",
        "feeToken": "USDC",
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


def test_a_filled_placement_whose_fills_read_fails_names_a_fills_failure_not_a_place() -> None:
    # The order filled, but the follow-up fills read dies in transport. The
    # placement itself succeeded, so naming it a *place* failure would mislead
    # triage — name it a fills-read failure, emit nothing, and let
    # reconciliation's fetch_order re-read FILLED and heal the fills (R004).
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
    assert failed and failed[0]["request"] == "fills"


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


async def fetch_view(post: FakeExchangeApi) -> VenueOrderView | None:
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

    assert view is not None
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

    assert view is not None
    assert not view.has_record
    assert view.status is None
    assert view.fills == ()


def test_fetch_order_returns_none_when_the_read_itself_fails() -> None:
    # The connectivity guard (ADR-0011 inv 1): a timeout or transport error is
    # None — never an empty view, which would read as "no record" and let
    # recovery resend into an outage.
    for failure in (TimeoutError("venue timed out"), ConnectionError("connection refused")):
        view = asyncio.run(fetch_view(FakeExchangeApi({"orderStatus": failure})))
        assert view is None

    # A failure on the *fills* half of the read poisons the whole view too.
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
    assert view is None


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

    assert view is None
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

    assert view is not None and view.status is not None
    assert view.status.status is state


def test_fetch_order_treats_a_status_it_cannot_map_as_a_failed_read() -> None:
    # A venue status outside the known taxonomy (say, a trigger state v1 never
    # places) must freeze the reconciler, not get misclassified as venue truth.
    view = asyncio.run(
        fetch_view(FakeExchangeApi({"orderStatus": order_status_response(status="triggered")}))
    )
    assert view is None


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


def test_connecting_asks_the_venue_for_nothing_because_posting_is_request_scoped() -> None:
    """``start()`` is the connect half of ADR-0024 step 4. This adapter holds no
    connection of its own — every request is scoped to the call that makes it —
    so the step does no venue I/O until ADR-0046's account-mode gate and
    ADR-0044's leverage push give it some.

    The fake routes *nothing*, so a request of any type fails loudly here rather
    than passing on a response a test never described. That is the assertion:
    when the push arrives, this test must be rewritten, not silently survive."""
    post = FakeExchangeApi({})
    exchange = make_exchange(post, bus=InMemoryBus(), clock=ManualClock())

    asyncio.run(exchange.start())

    assert post.requests == []


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
            {"order": resting_response(oid=77), "cancelByCloid": cancel_success_response()}
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

    assert [payload["action"]["type"] for (_url, payload) in post.requests] == [
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
    "start": "test_connecting_asks_the_venue_for_nothing_because_posting_is_request_scoped",
    "stop": "test_the_venue_link_is_released_without_a_start_having_run",
    "place": "test_market_buy_places_an_aggressive_ioc_limit_at_the_bounded_price",
    "cancel": "test_cancel_sends_a_signed_cancel_by_cloid_and_reports_cancelled",
    "fetch_order": "test_fetch_order_bundles_the_venue_status_and_fills_into_one_view",
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
