"""The connectivity guard, end to end (issue #23): a venue outage freezes the
real ``Reconciler`` against the real ``HyperliquidExchange``.

Only the HTTP transport is fake (and dark). The adapter's ``fetch_order``
answers ``None`` for a failed read (ADR-0011 inv 1), and the reconciler must
freeze on it — no synthetic events, no resend — rather than mistake the
outage for "no record".
"""

import asyncio
from decimal import Decimal

import pytest
from hyperliquid_fakes import FakeExchangeApi
from pydantic import SecretStr

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    ExecutionReport,
    InstrumentSpec,
    Order,
    OrderState,
    OrderStatusReport,
    OrderType,
    Side,
    VenueFactUnsupported,
)
from tickwright.engine.cache import Cache
from tickwright.engine.reconcile import ReconcileConfig, Reconciler
from tickwright.observability import NamedEvent
from tickwright.observability.testing import capture_events
from tickwright.venues.hyperliquid import (
    HyperliquidConfig,
    HyperliquidExchange,
    HyperliquidUniverse,
)

# Anvil's account #0 — a publicly-known throwaway key, safe in a test file.
TEST_SIGNING_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"

BTC_SPEC = InstrumentSpec(
    symbol="BTC",
    sz_decimals=5,
    max_decimals=6,
    min_notional=Decimal("10"),
    max_sig_figs=5,
)


POISON = "0x" + "aa" * 16
HEALTHY = "0x" + "bb" * 16


def _saga(cloid: str, state: OrderState) -> Order:
    order = Order(
        cloid=cloid,
        strategy_id="live",
        signal_id=f"live:BTC:{cloid[-2:]}",
        symbol="BTC",
        side=Side.BUY,
        quantity=Decimal("0.5"),
        order_type=OrderType.LIMIT,
    )
    order.state = state
    return order


def _submitted_saga() -> Order:
    return _saga("0x" + "ab" * 16, OrderState.SUBMITTED)


def _order_status_body(*, oid: int, status: str, cloid: str) -> dict:
    """The venue's ``orderStatus`` answer for a known order."""
    return {
        "status": "order",
        "order": {
            "order": {
                "coin": "BTC",
                "oid": oid,
                "timestamp": 1_700_000_000_000,
                "cloid": cloid,
            },
            "status": status,
        },
    }


def _make_exchange(post: FakeExchangeApi, bus: InMemoryBus, clock: ManualClock):
    return HyperliquidExchange(
        config=HyperliquidConfig(
            testnet=True, symbols=["BTC"], signing_key=SecretStr(TEST_SIGNING_KEY)
        ),
        bus=bus,
        clock=clock,
        universe=HyperliquidUniverse(specs={"BTC": BTC_SPEC}, asset_indices={"BTC": 3}),
        post=post,
        startup_timeout_seconds=60.0,
    )


def _poisoned_book() -> FakeExchangeApi:
    """Two resting orders, the first answering a status the saga cannot map.

    A valid envelope carrying a status string outside the taxonomy: the body
    parsed, so this is not an outage — the venue is up and answering, and the
    second order's read is one ordinary round-trip away.
    """
    statuses = {
        POISON: _order_status_body(oid=91, status="liquidatedByTheVenue", cloid=POISON),
        HEALTHY: _order_status_body(oid=92, status="canceled", cloid=HEALTHY),
    }
    return FakeExchangeApi(
        {
            "orderStatus": lambda payload: statuses[payload["oid"]],
            "userFillsByTime": [],
        }
    )


def test_an_unreadable_body_stops_its_own_order_and_not_the_ones_behind_it() -> None:
    # Issue #236. One order's body is unreadable; the order checkpointed behind
    # it is perfectly readable. The venue answered both — a dead transport is
    # the *other* failure — so stopping the pass at the first one buys nothing
    # and costs everything behind it, on every cycle, for as long as the venue
    # keeps returning the same stored string.
    async def main() -> tuple[bool, list[ExecutionReport], FakeExchangeApi]:
        bus = InMemoryBus()
        clock = ManualClock(start_ns=0)
        post = _poisoned_book()
        exchange = _make_exchange(post, bus, clock)
        store = SQLiteStore(":memory:")
        store.checkpoint(_saga(POISON, OrderState.LIVE), ts_ns=500)
        store.checkpoint(_saga(HEALTHY, OrderState.LIVE), ts_ns=500)
        cache = Cache(store=store)
        cache.rebuild()
        reconciler = Reconciler(
            bus=bus, clock=clock, exchange=exchange, cache=cache, config=ReconcileConfig()
        )

        reports: list[ExecutionReport] = []

        async def collect(report: ExecutionReport) -> None:
            reports.append(report)

        bus.subscribe(ExecutionReport, collect)
        cleared = await reconciler.reconcile_open_orders()
        return cleared, reports, post

    with capture_events() as events:
        cleared, reports, post = asyncio.run(main())

    # The healthy order was read at all — the poisoned one no longer answers
    # for it — and reconciled to the venue's own truth.
    read_oids = [
        payload["oid"] for (_, payload) in post.requests if payload["type"] == "orderStatus"
    ]
    assert read_oids == [POISON, HEALTHY]
    (healed,) = reports
    assert isinstance(healed, OrderStatusReport)
    assert (healed.cloid, healed.status) == (HEALTHY, OrderState.CANCELLED)

    # The pass is still not a clean one: one order could not be proven, so the
    # verdict stays False and the freeze is named against that order.
    assert cleared is False
    failed = [e for e in events if e["event"] == NamedEvent.EXCHANGE_REQUEST_FAILED]
    assert [e["cloid"] for e in failed] == [POISON]


def test_a_venue_outage_freezes_reconciliation_instead_of_resolving_inflight() -> None:
    async def main() -> tuple[bool, list[ExecutionReport]]:
        bus = InMemoryBus()
        clock = ManualClock(start_ns=0)
        # Every orderStatus read the reconciler makes dies in transport: the
        # outage case (one route answers them all — the venue's state holds).
        post = FakeExchangeApi({"orderStatus": ConnectionError("venue unreachable")})
        exchange = HyperliquidExchange(
            config=HyperliquidConfig(
                testnet=True, symbols=["BTC"], signing_key=SecretStr(TEST_SIGNING_KEY)
            ),
            bus=bus,
            clock=clock,
            universe=HyperliquidUniverse(specs={"BTC": BTC_SPEC}, asset_indices={"BTC": 3}),
            post=post,
            startup_timeout_seconds=60.0,
        )
        store = SQLiteStore(":memory:")
        store.checkpoint(_submitted_saga(), ts_ns=500)
        cache = Cache(store=store)
        cache.rebuild()
        reconciler = Reconciler(
            bus=bus, clock=clock, exchange=exchange, cache=cache, config=ReconcileConfig()
        )

        reports: list[ExecutionReport] = []

        async def collect(report: ExecutionReport) -> None:
            reports.append(report)

        bus.subscribe(ExecutionReport, collect)
        cleared = await reconciler.reconcile_inflight()
        return cleared, reports

    with capture_events() as events:
        cleared, reports = asyncio.run(main())

    # Frozen: the in-flight saga stays unresolved (no barrier clear), nothing
    # synthetic reaches the bus, and the freeze is named for triage.
    assert cleared is False
    assert reports == []
    assert any(e["event"] == NamedEvent.RECONCILE_FROZEN for e in events)


def test_a_permanent_refusal_leaves_the_cycle_instead_of_freezing_it_forever() -> None:
    # The other half of the guard, and the reason it needs two outcomes and not
    # one (ADR-0048). A fee settled in a token this ledger cannot hold is not an
    # outage: the venue's fill row is already settled, so every later pass reads
    # it to the same refusal. Answered with the `None` above it would freeze this
    # cycle on every tick forever — and `_drive` returns on the first frozen read,
    # so every order behind this one in the iteration would stop reconciling too,
    # with one `RECONCILE_FROZEN` a cycle the only trace. Nothing between the
    # adapter and the runner may absorb it: it has to reach the TaskGroup and
    # fault the engine, where an operator can see it (ADR-0036 §4).
    async def main() -> None:
        bus = InMemoryBus()
        clock = ManualClock(start_ns=0)
        post = FakeExchangeApi(
            {
                "orderStatus": {
                    "status": "order",
                    "order": {
                        "order": {
                            "coin": "BTC",
                            "oid": 91,
                            "timestamp": 1_700_000_000_000,
                            "cloid": "0x" + "ab" * 16,
                        },
                        "status": "filled",
                    },
                },
                "userFillsByTime": [
                    {
                        "coin": "BTC",
                        "px": "43250.0",
                        "sz": "0.5",
                        "time": 1_700_000_000_500,
                        "oid": 91,
                        "fee": "0.02",
                        "feeToken": "HYPE",
                        "tid": 556,
                    }
                ],
            }
        )
        exchange = HyperliquidExchange(
            config=HyperliquidConfig(
                testnet=True, symbols=["BTC"], signing_key=SecretStr(TEST_SIGNING_KEY)
            ),
            bus=bus,
            clock=clock,
            universe=HyperliquidUniverse(specs={"BTC": BTC_SPEC}, asset_indices={"BTC": 3}),
            post=post,
            startup_timeout_seconds=60.0,
        )
        store = SQLiteStore(":memory:")
        store.checkpoint(_submitted_saga(), ts_ns=500)
        cache = Cache(store=store)
        cache.rebuild()
        reconciler = Reconciler(
            bus=bus, clock=clock, exchange=exchange, cache=cache, config=ReconcileConfig()
        )
        await reconciler.reconcile_inflight()

    with capture_events() as events:
        with pytest.raises(VenueFactUnsupported, match="HYPE"):
            asyncio.run(main())

    # It escalated rather than freezing: a freeze here is the failure mode this
    # test exists to catch, because it is the one that looks like nothing.
    assert not any(e["event"] == NamedEvent.RECONCILE_FROZEN for e in events)
