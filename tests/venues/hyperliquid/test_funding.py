"""Live funding ingest — the venue's own payments, taken verbatim (ADR-0037).

The expected amounts here are the venue's **reported field**, not a number this
suite recomputed: `-3.625312` is quoted from the `userFunding` example in
`docs/research/hyperliquid-perp-fees-funding.md`, whose worked case is a long
`szi=+49.1477` at rate `+0.0000417`. Re-deriving it with paper's formula would
make the test agree with the code by construction and leave a sign flip
invisible, which is the one bug this module exists not to have.
"""

import asyncio
import json
from decimal import Decimal

import pytest
from eth_account import Account
from hyperliquid_fakes import FakeExchangeApi, FakeWsConnection, user_fundings_frame
from pydantic import SecretStr

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import AccountSpec, FundingAccrual, InstrumentSpec, VenueFactUnsupported
from tickwright.engine.checkpoint import Checkpointer
from tickwright.engine.portfolio import PortfolioProjection
from tickwright.venues.hyperliquid import (
    HyperliquidConfig,
    HyperliquidExchange,
    HyperliquidUniverse,
)
from tickwright.venues.hyperliquid.funding import accruals
from tickwright.venues.hyperliquid.transport import Connect

_NS_PER_MS = 1_000_000

# Anvil's account #0 — a publicly-known throwaway key, safe in a test file.
TEST_SIGNING_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
ANVIL_ADDRESS = Account.from_key(TEST_SIGNING_KEY).address

ETH_SPEC = InstrumentSpec(
    symbol="ETH", sz_decimals=4, max_decimals=6, min_notional=Decimal("10"), max_sig_figs=5
)
UNIVERSE = HyperliquidUniverse(specs={"ETH": ETH_SPEC}, asset_indices={"ETH": 1})


def make_exchange(
    post: FakeExchangeApi, *, bus: InMemoryBus, clock: ManualClock, connect: Connect
) -> HyperliquidExchange:
    config = HyperliquidConfig(
        testnet=True,
        symbols=["ETH"],
        signing_key=SecretStr(TEST_SIGNING_KEY),
        slippage_bound=Decimal("0.05"),
    )
    return HyperliquidExchange(
        config=config,
        bus=bus,
        clock=clock,
        universe=UNIVERSE,
        post=post,
        connect=connect,
        startup_timeout_seconds=60.0,
    )


def funding(*, time_ms: int, coin: str, usdc: str, szi: str = "1", rate: str = "0.0000417") -> dict:
    """One `WsUserFunding` record, the venue's flat per-payment shape."""
    return {"time": time_ms, "coin": coin, "usdc": usdc, "szi": szi, "fundingRate": rate}


def test_a_reported_payment_becomes_an_accrual_with_the_venue_s_own_amount_and_time() -> None:
    reported = funding(time_ms=1681222254710, coin="ETH", usdc="-3.625312", szi="49.1477")

    (accrual,) = accruals([reported], account_id="hyperliquid-mainnet-0xabc", ts_init_ns=42)

    assert isinstance(accrual, FundingAccrual)
    assert accrual.account_id == "hyperliquid-mainnet-0xabc"
    assert accrual.symbol == "ETH"
    # Verbatim, sign included: negative is funding paid (ADR-0037 §Sign).
    assert accrual.amount == Decimal("-3.625312")
    assert accrual.boundary_ts_ns == 1681222254710 * _NS_PER_MS
    assert accrual.ts_event == 1681222254710 * _NS_PER_MS
    assert accrual.ts_init == 42


def test_a_received_payment_keeps_the_venue_s_positive_sign() -> None:
    """The other half of the sign, so a `abs()` or a negation cannot pass.

    A short at a positive rate is paid, and the venue reports that as a positive
    `usdc`. Nothing on this path may transform it.
    """
    reported = funding(time_ms=1681225854710, coin="BTC", usdc="3.625312", szi="-49.1477")

    (accrual,) = accruals([reported], account_id="hyperliquid-mainnet-0xabc", ts_init_ns=42)

    assert accrual.amount == Decimal("3.625312")


def test_an_out_of_order_batch_is_emitted_in_venue_time_order() -> None:
    """The module's one piece of real content (ADR-0043 §5.2, module map).

    The venue documents no delivery order within a batch, and the projection's
    watermark gate is monotonic: fed `t3, t1, t2` unsorted it would apply `t3`,
    advance the mark past it, then drop `t1` and `t2` as already-applied. Those
    are real payments the account never sees again — the snapshot is delivered
    once. Sorting here is the only place that ordering can be established.
    """
    shuffled = [
        funding(time_ms=1681229454710, coin="ETH", usdc="-1"),
        funding(time_ms=1681222254710, coin="ETH", usdc="-3"),
        funding(time_ms=1681225854710, coin="ETH", usdc="-2"),
    ]

    emitted = accruals(shuffled, account_id="hyperliquid-mainnet-0xabc", ts_init_ns=42)

    assert [a.boundary_ts_ns for a in emitted] == [
        1681222254710 * _NS_PER_MS,
        1681225854710 * _NS_PER_MS,
        1681229454710 * _NS_PER_MS,
    ]
    # Each amount still rides its own boundary: a sort that reordered one and
    # not the other would pass the assertion above.
    assert [a.amount for a in emitted] == [Decimal("-3"), Decimal("-2"), Decimal("-1")]


def test_payments_settling_on_one_boundary_keep_the_venue_s_delivery_order() -> None:
    """Every symbol settles on the same hourly boundary, so same-`time` records
    are the common case rather than a corner.

    The sort is stable, which leaves their relative order the venue's. There is
    nothing better to order them by — they are distinct payments on distinct
    symbols, keyed apart by `symbol`, so the watermark treats them as one
    boundary either way and any reshuffle would be this module inventing a
    sequence the venue did not report.
    """
    same_boundary = [
        funding(time_ms=1681222254710, coin="SOL", usdc="-1"),
        funding(time_ms=1681222254710, coin="ETH", usdc="-2"),
        funding(time_ms=1681222254710, coin="BTC", usdc="-3"),
    ]

    emitted = accruals(same_boundary, account_id="hyperliquid-mainnet-0xabc", ts_init_ns=42)

    assert [a.symbol for a in emitted] == ["SOL", "ETH", "BTC"]


def test_a_payment_not_denominated_in_usdc_raises_rather_than_accruing() -> None:
    """ADR-0029's assumption, guarded where the venue lets it be guarded.

    A funding record carries no currency discriminator the way a fill carries
    `feeToken` — the amount field is *named* `usdc`, so the denomination is the
    key itself. A payment settled in anything else therefore does not arrive as
    a `usdc` this suite could set to another token; it arrives as a record with
    no `usdc` at all.

    The failure that matters is the silent one: `.get("usdc", 0)` would accrue a
    zero, and a zero is indistinguishable from a boundary that genuinely owed
    nothing — the account would under-count real money with nothing recording
    that it had. So this refuses, naming the keys the record did carry, which is
    the one thing telling an operator what the venue actually sent.
    """
    settled_elsewhere = {
        "time": 1681222254710,
        "coin": "ETH",
        "hype": "-3.625312",
        "szi": "49.1477",
        "fundingRate": "0.0000417",
    }

    with pytest.raises(VenueFactUnsupported) as raised:
        accruals([settled_elsewhere], account_id="hyperliquid-mainnet-0xabc", ts_init_ns=42)

    message = str(raised.value)
    assert "ETH" in message
    assert "hype" in message


def test_a_refused_record_takes_the_whole_batch_with_it() -> None:
    """Funding is money, so this module's frame policy is the *opposite* of the
    feed's.

    `feed.py` drops a malformed row and names it, because the tick stream is
    lossy by contract (ADR-0023) and the next trade is along in a moment. A
    funding payment has no next: the snapshot is delivered once, and a dropped
    row is cash the ledger never learns about. Refusing the batch escalates out
    of the seam and faults the run (ADR-0036 §4) rather than banking the readable
    part of a delivery we did not fully understand — which would also advance the
    watermark past the row that was skipped.
    """
    batch = [
        funding(time_ms=1681222254710, coin="ETH", usdc="-3.625312"),
        {"time": 1681225854710, "coin": "BTC", "szi": "1", "fundingRate": "0.0000417"},
    ]

    with pytest.raises(VenueFactUnsupported):
        accruals(batch, account_id="hyperliquid-mainnet-0xabc", ts_init_ns=42)


def _ingest(frames: list[str], *, until: int) -> tuple[list[FundingAccrual], FakeWsConnection]:
    """Run the live exchange's funding ingest over ``frames`` until ``until``
    accruals reach the bus, then stop it.

    The seam is the `Exchange` protocol, not the module function two tests up:
    what this asserts is that the *adapter* subscribes, decodes and publishes on
    the same bus variant paper's generator publishes to, so the projection's
    apply path stays free of any `if live:`.
    """

    async def main() -> tuple[list[FundingAccrual], FakeWsConnection]:
        bus = InMemoryBus()
        clock = ManualClock(start_ns=7)
        seen: list[FundingAccrual] = []
        enough = asyncio.Event()

        async def record(accrual: FundingAccrual) -> None:
            seen.append(accrual)
            if len(seen) >= until:
                enough.set()

        bus.subscribe(FundingAccrual, record)
        connection = FakeWsConnection(frames)

        async def connect(url: str) -> FakeWsConnection:
            return connection

        exchange = make_exchange(FakeExchangeApi({}), bus=bus, clock=clock, connect=connect)
        async with asyncio.TaskGroup() as tg:
            running = tg.create_task(exchange.run())
            await asyncio.wait_for(enough.wait(), timeout=2)
            await exchange.stop()
            await running
        return seen, connection

    return asyncio.run(main())


def test_the_live_adapter_subscribes_to_its_own_account_s_funding_channel() -> None:
    frames = [user_fundings_frame(funding(time_ms=1681222254710, coin="ETH", usdc="-3"))]

    _, connection = _ingest(frames, until=1)

    (sent,) = connection.sent
    assert json.loads(sent) == {
        "method": "subscribe",
        "subscription": {"type": "userFundings", "user": ANVIL_ADDRESS},
    }


def test_the_live_adapter_publishes_the_venue_s_payments_on_the_bus() -> None:
    """The `run()` half of ADR-0037's pair: live ingests where paper generates,
    and both reach the projection as the same event on the same bus."""
    frames = [
        user_fundings_frame(
            funding(time_ms=1681225854710, coin="ETH", usdc="-2"),
            funding(time_ms=1681222254710, coin="ETH", usdc="-3"),
            snapshot=True,
        ),
        user_fundings_frame(funding(time_ms=1681229454710, coin="ETH", usdc="-1")),
    ]

    seen, _ = _ingest(frames, until=3)

    # The snapshot's own batch is ordered, and the streamed payment that follows
    # it is ingested by the identical path — a payment is a payment however the
    # venue chose to deliver it.
    assert [a.amount for a in seen] == [Decimal("-3"), Decimal("-2"), Decimal("-1")]
    assert [a.boundary_ts_ns for a in seen] == [
        1681222254710 * _NS_PER_MS,
        1681225854710 * _NS_PER_MS,
        1681229454710 * _NS_PER_MS,
    ]
    assert {a.account_id for a in seen} == {f"hyperliquid-testnet-{ANVIL_ADDRESS}"}
    # ``ts_init`` is when this process built the object, off the injected clock —
    # distinct from the venue's settlement instant, which is ``ts_event``.
    assert {a.ts_init for a in seen} == {7}


LIVE_ACCOUNT = f"hyperliquid-testnet-{ANVIL_ADDRESS}"

# The boundaries every case below is built from — one hour apart, as the venue
# settles them, and quoted here once so no case declares them twice.
FIRST_MS, SECOND_MS = 1681222254710, 1681225854710
THIRD_MS, FOURTH_MS = 1681229454710, 1681233054710


def _ingest_refusing(frames: list[str]) -> str:
    """Drive the live adapter over ``frames`` and return the refusal it raised.

    No stop and no completion event: these frames are the ones that must never
    reach the bus, so the assertion is that `run()` itself comes back raising.
    """

    async def main() -> None:
        bus = InMemoryBus()
        clock = ManualClock(start_ns=7)
        connection = FakeWsConnection(frames)

        async def connect(url: str) -> FakeWsConnection:
            return connection

        exchange = make_exchange(FakeExchangeApi({}), bus=bus, clock=clock, connect=connect)
        await asyncio.wait_for(exchange.run(), timeout=2)

    with pytest.raises(VenueFactUnsupported) as raised:
        asyncio.run(main())
    return str(raised.value)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param([{"time": FIRST_MS, "coin": "ETH", "usdc": "-3"}], id="rest-shaped-list"),
        pytest.param({"isSnapshot": False, "user": "0xuser"}, id="no-fundings-key"),
        pytest.param(
            {"isSnapshot": False, "fundings": {"time": FIRST_MS}}, id="fundings-not-a-list"
        ),
    ],
)
def test_a_funding_delivery_whose_body_is_unreadable_refuses_rather_than_ingesting_nothing(
    body: object,
) -> None:
    """The frame grain of the refusal the record grain already has.

    A frame on this channel *is* a delivery of payments, so a body this process
    cannot read as a batch is not "no payments" — it is an unknown number of
    them, and answering it with an empty tuple books that unknown as zero. The
    account then under-counts real money with nothing recording that it had,
    which is the same silent failure `_settled_in_usdc` refuses one grain down
    and the failure ADR-0037 §2's amendment designs against.

    Nothing here can be healed by waiting, either: a websocket message is
    delivered whole or not at all (RFC 6455 §5.4 fragmentation, reassembled by
    the client), so a body that arrives unreadable is the venue's contract having
    changed rather than a truncation a re-read would fix.
    """
    message = _ingest_refusing([json.dumps({"channel": "userFundings", "data": body})])

    assert "userFundings" in message


@pytest.mark.parametrize("frame", ['[{"time": 1}]', '"fundings"', "17", "null", "<html>502</html>"])
def test_a_frame_that_names_no_channel_refuses_on_the_money_socket(frame: str) -> None:
    """The feed drops these and names them; this socket cannot.

    A frame that is not a JSON object carries no `channel`, so nothing about it
    says it was *not* the funding delivery this subscription exists to read —
    and on a channel where every message is cash, "we could not tell" has to
    fail closed. `feed.py` may drop its unparseable frames because the tick
    stream is lossy by contract (ADR-0023) and another tick is along in a
    moment; a payment has no next delivery.
    """
    message = _ingest_refusing([frame])

    assert "funding" in message


def test_a_dropped_socket_resubscribes_and_the_re_delivered_snapshot_heals_the_gap() -> None:
    """Why this subscription can afford to lose a socket at all.

    Resubscribing re-delivers the whole snapshot, so the payments that settled
    while the connection was down arrive with the ones already applied — the
    watermark drops those and keeps these, which is what makes a reconnect
    *heal* the gap rather than leave one (ADR-0043 §5.2). The claim was in the
    module's docstring with nothing driving it: the ingest owns the resubscribe,
    and the gate behind it is asserted separately by the re-ingest case below.
    """
    dropped = FakeWsConnection(
        [user_fundings_frame(funding(time_ms=FIRST_MS, coin="ETH", usdc="-3"), snapshot=True)],
        drop_when_drained=True,
    )
    recovered = FakeWsConnection(
        [
            user_fundings_frame(
                funding(time_ms=FIRST_MS, coin="ETH", usdc="-3"),
                funding(time_ms=SECOND_MS, coin="ETH", usdc="-2"),
                snapshot=True,
            )
        ]
    )

    async def main() -> list[FundingAccrual]:
        bus = InMemoryBus()
        clock = ManualClock(start_ns=7)
        seen: list[FundingAccrual] = []
        enough = asyncio.Event()

        async def record(accrual: FundingAccrual) -> None:
            seen.append(accrual)
            if len(seen) >= 3:
                enough.set()

        bus.subscribe(FundingAccrual, record)
        sockets: list[FakeWsConnection] = [dropped, recovered]

        async def connect(url: str) -> FakeWsConnection:
            return sockets.pop(0)

        exchange = make_exchange(FakeExchangeApi({}), bus=bus, clock=clock, connect=connect)
        async with asyncio.TaskGroup() as tg:
            running = tg.create_task(exchange.run())
            await asyncio.wait_for(enough.wait(), timeout=2)
            await exchange.stop()
            await running
        return seen

    seen = asyncio.run(main())

    # The boundary missed while the socket was down is delivered by the new one,
    # alongside the re-delivery of the one already applied.
    assert [a.boundary_ts_ns for a in seen] == [
        FIRST_MS * _NS_PER_MS,
        FIRST_MS * _NS_PER_MS,
        SECOND_MS * _NS_PER_MS,
    ]
    # Both sockets were subscribed: a recovered connection nobody subscribed
    # would sit open and silent, which is the gap this is claiming to close.
    assert len(dropped.sent) == len(recovered.sent) == 1


def test_the_venue_s_own_housekeeping_frames_are_ignored_rather_than_refused() -> None:
    """The other half of the rule, deliberately adjacent to the two above.

    `subscriptionResponse` and `pong` ride the same socket, name a channel that
    is not this one, and are constant traffic — they were never a delivery of
    payments, so there is nothing here to fail closed about. Keeping the cases
    together is what stops the two policies being re-merged later: "ignore" and
    "refuse" are deliberately different answers to superficially similar frames.
    """
    frames = [
        json.dumps({"channel": "subscriptionResponse", "data": {"method": "subscribe"}}),
        json.dumps({"channel": "pong"}),
        user_fundings_frame(funding(time_ms=FIRST_MS, coin="ETH", usdc="-3")),
    ]

    seen, _ = _ingest(frames, until=1)

    assert [a.amount for a in seen] == [Decimal("-3")]


def _ingest_into_ledger(
    frames: list[str], *, until: int, store: SQLiteStore
) -> PortfolioProjection:
    """Run the live ingest over ``frames`` into a ledger recovered from ``store``.

    The composition the acceptance criteria are about, and the only place it
    exists: this module's batch sort meeting the durable watermark that guards
    the ledger. Neither half can be asserted alone — the sort is correct against
    a gate it never sees, and the gate is correct against an order it never
    establishes.

    The subscriber is ``Checkpointer.checkpoint_funding`` because that is what
    the runner's ``_on_funding_accrual`` calls and all it does; the *wiring* of
    bus to verb is asserted at the engine's own seam
    (``tests/engine/test_funding_e2e.py``) rather than restated here.

    The ledger is **recovered** rather than freshly opened, so a second call over
    the same store is a genuine restart: the watermark the gate reads is then the
    durable one, and a re-delivery case cannot pass on a value the ingest that
    wrote it happened to leave in memory.

    ``until`` counts accruals **delivered**, not applied — a batch the gate drops
    entirely still reaches the bus, so counting applications would hang exactly
    the case that asserts nothing is applied.
    """

    async def main() -> PortfolioProjection:
        bus = InMemoryBus()
        clock = ManualClock(start_ns=7)
        # Live declares no genesis: the opening balance is ingested from the
        # venue at the startup barrier (ADR-0042 §6), so this ledger opens at
        # zero and the cash line below is funding and nothing else.
        checkpointer = Checkpointer(
            spec=AccountSpec(account_id=LIVE_ACCOUNT, genesis_collateral=None),
            store=store,
            clock=clock,
        )
        checkpointer.portfolio.recover()
        delivered = 0
        enough = asyncio.Event()

        async def book(accrual: FundingAccrual) -> None:
            nonlocal delivered
            checkpointer.checkpoint_funding(accrual)
            delivered += 1
            if delivered >= until:
                enough.set()

        bus.subscribe(FundingAccrual, book)
        connection = FakeWsConnection(frames)

        async def connect(url: str) -> FakeWsConnection:
            return connection

        exchange = make_exchange(FakeExchangeApi({}), bus=bus, clock=clock, connect=connect)
        async with asyncio.TaskGroup() as tg:
            running = tg.create_task(exchange.run())
            await asyncio.wait_for(enough.wait(), timeout=2)
            await exchange.stop()
            await running
        return checkpointer.portfolio

    return asyncio.run(main())


def _batch(*amounts_by_ms: tuple[int, str], snapshot: bool = True) -> list[str]:
    """One `userFundings` frame carrying `(boundary_ms, usdc)` payments on ETH."""
    return [
        user_fundings_frame(
            *(funding(time_ms=time_ms, coin="ETH", usdc=usdc) for time_ms, usdc in amounts_by_ms),
            snapshot=snapshot,
        )
    ]


def test_an_out_of_order_batch_applies_every_payment_in_it() -> None:
    """The slice's headline acceptance criterion (issue #192).

    The venue documents no delivery order within a batch, and the watermark is
    monotonic, so an unsorted delivery is not merely untidy — it is lossy. Fed
    `t3, t1, t2` the gate would apply `-1`, advance the mark past `t3`, and then
    drop `-3` and `-2` as already-applied: a cash line reading `-1` against `-6`
    genuinely paid, on a snapshot the venue delivers once and never repeats.

    So the two numbers below are the whole point of the case. `-6` is the sum of
    the venue's own three reported amounts; the failure it is placed against
    reads `-1`.
    """
    store = SQLiteStore(":memory:")
    shuffled = _batch((THIRD_MS, "-1"), (FIRST_MS, "-3"), (SECOND_MS, "-2"))

    ledger = _ingest_into_ledger(shuffled, until=3, store=store)

    assert ledger.account().cash == Decimal("-6")
    # The mark lands on the batch's *latest* boundary, which is only true if the
    # batch was applied in order — the gate advances it per accrual applied.
    assert store.funding_mark("ETH") == THIRD_MS * _NS_PER_MS


def test_a_re_ingested_batch_below_the_watermark_applies_nothing() -> None:
    """Reconnect is not a second payment (ADR-0043 §5.2).

    Resubscribing re-delivers the whole snapshot — that is the venue's design and
    the reason a dropped socket *heals* rather than leaving a gap. The same
    re-delivery arrives from live's reconcile re-ingest. So the batch below is
    ingested twice against one store, and the second pass must move nothing: a
    gate that failed here would double every payment on every reconnect, which is
    an accounting error that compounds silently for as long as the run lasts.

    The second ledger is **recovered from the store**, not the object the first
    pass left behind, so what stops it is the durable mark rather than anything
    remembered in memory. That is the whole point of the watermark being a store
    read: an in-memory key set dies with the process, and a restart is exactly
    when a re-delivery arrives.
    """
    store = SQLiteStore(":memory:")
    batch = _batch((THIRD_MS, "-1"), (FIRST_MS, "-3"), (SECOND_MS, "-2"))

    _ingest_into_ledger(batch, until=3, store=store)
    replayed = _ingest_into_ledger(batch, until=3, store=store)

    # -6, not -12: the restart read the durable cash line back, and the gate
    # dropped all three re-delivered payments on top of it.
    assert replayed.account().cash == Decimal("-6")
    assert store.funding_mark("ETH") == THIRD_MS * _NS_PER_MS


def test_a_batch_straddling_the_watermark_applies_exactly_what_is_above_it() -> None:
    """The realistic reconnect, and the case the other two cannot reach between
    them.

    A socket that drops mid-run comes back to a snapshot spanning boundaries the
    ledger already has *and* boundaries it missed while it was down — so the
    gate's job is not to accept a batch or reject it, but to cut it. Both
    all-or-nothing cases would still pass a gate that only ever did one of those:
    an always-drop keeps the re-delivery case green, and an always-apply keeps
    the out-of-order case green.

    So the arithmetic is the assertion. Two boundaries are already applied at -5;
    the redelivery brings those two back and adds -1 and -4 above them. -10 is
    the only reading that means *exactly* the new pair landed — an ungated pass
    reads -15, and a gate that dropped the whole straddling batch reads -5.
    """
    store = SQLiteStore(":memory:")
    _ingest_into_ledger(_batch((FIRST_MS, "-3"), (SECOND_MS, "-2")), until=2, store=store)
    assert store.funding_mark("ETH") == SECOND_MS * _NS_PER_MS

    straddling = _batch((FIRST_MS, "-3"), (SECOND_MS, "-2"), (THIRD_MS, "-1"), (FOURTH_MS, "-4"))
    healed = _ingest_into_ledger(straddling, until=4, store=store)

    assert healed.account().cash == Decimal("-10")
    assert store.funding_mark("ETH") == FOURTH_MS * _NS_PER_MS
