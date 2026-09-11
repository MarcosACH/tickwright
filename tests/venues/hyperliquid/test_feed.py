"""``HyperliquidFeed`` — recorded-frame tests, no network (ADR-0021/0023/0027).

The WS connection is the process boundary: ``FakeWsConnection`` replays
recorded ``trades``-channel frames (captured shape, see ``fixtures/``) and
records what the feed sends, so the default suite drives the live-feed code
path hermetically.
"""

import asyncio
import contextlib
import json
from decimal import Decimal
from pathlib import Path

import pytest
from feed_contract import (
    MarketDataTranscript,
    assert_every_traded_symbol_is_marked,
    record_market_data,
)
from hyperliquid_fakes import (
    FakeWsConnection,
    RecordingClock,
    asset_ctx_frame,
    trade,
    trades_frame,
)
from seam_claims import assert_every_member_is_claimed
from structlog.typing import EventDict

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.domain import AggressorSide, MarketFeed, MarketTick, MarkTick
from tickwright.observability.testing import capture_events
from tickwright.venues.hyperliquid import HyperliquidConfig, HyperliquidFeed

_FIXTURES = Path(__file__).parent / "fixtures"


def _fixture_frames() -> list[str]:
    text = (_FIXTURES / "trades.jsonl").read_text(encoding="utf-8")
    return [line for line in text.splitlines() if line.strip()]


def _run_feed[E: (MarketTick, MarkTick)](
    frames: list[str],
    *,
    symbols: list[str],
    event_type: type[E],
    until: int,
    clock: ManualClock,
) -> tuple[list[E], FakeWsConnection]:
    """Run the feed over ``frames`` until ``until`` events of ``event_type``
    arrive, then stop.

    Parameterized on the event type rather than copied per stream: the two the
    feed sources reach the bus down one identical connect/subscribe/read/drain
    path, so a driver per stream would be the same thirty lines asserting
    nothing the other does not. A third stream costs the wrapper below, not
    another copy of this.
    """

    async def main() -> tuple[list[E], FakeWsConnection]:
        bus = InMemoryBus()
        seen: list[E] = []
        enough = asyncio.Event()

        async def record(event: E) -> None:
            seen.append(event)
            if len(seen) >= until:
                enough.set()

        bus.subscribe(event_type, record)
        connection = FakeWsConnection(frames)

        feed = HyperliquidFeed(
            config=HyperliquidConfig(symbols=symbols),
            bus=bus,
            clock=clock,
            connect=connection.connect,
        )
        await feed.start()
        run = asyncio.create_task(feed.run())
        await asyncio.wait_for(enough.wait(), timeout=2)
        await feed.stop()
        await asyncio.wait_for(run, timeout=2)
        return seen, connection

    return asyncio.run(main())


def _drive(
    frames: list[str], *, symbols: list[str], until_ticks: int
) -> tuple[list[MarketTick], FakeWsConnection]:
    """Run the feed until ``until_ticks`` ticks arrive. A tick's ``ts_event`` is
    mapped from the venue's own ``time``, so the clock here is never read."""
    return _run_feed(
        frames, symbols=symbols, event_type=MarketTick, until=until_ticks, clock=ManualClock()
    )


def test_recorded_trades_frames_parse_into_market_ticks() -> None:
    seen, _ = _drive(_fixture_frames(), symbols=["BTC"], until_ticks=2)

    buy, sell = seen
    assert buy.symbol == "BTC"
    assert buy.price == Decimal("43250.5")
    assert buy.size == Decimal("0.25")
    assert buy.aggressor_side is AggressorSide.BUY
    assert buy.trade_id == "900000000000001"
    assert buy.ts_event == 1_700_000_000_123 * 1_000_000  # venue ms → engine ns

    assert sell.aggressor_side is AggressorSide.SELL
    assert sell.price == Decimal("43249.0")


def test_prices_and_sizes_parse_to_decimal_never_float() -> None:
    frames = [trades_frame(trade("BTC", "0.1", 1, sz="0.2"))]
    seen, _ = _drive(frames, symbols=["BTC"], until_ticks=1)

    assert isinstance(seen[0].price, Decimal)
    assert isinstance(seen[0].size, Decimal)
    assert seen[0].price == Decimal("0.1")  # exact — a float round-trip would not be


def test_batched_trades_frame_yields_one_tick_per_trade_in_order() -> None:
    frames = [trades_frame(trade("BTC", "100", 1), trade("BTC", "101", 2))]
    seen, _ = _drive(frames, symbols=["BTC"], until_ticks=2)

    assert [t.trade_id for t in seen] == ["1", "2"]


def test_assigns_per_symbol_source_sequence() -> None:
    frames = [
        trades_frame(trade("BTC", "100", 1)),
        trades_frame(trade("ETH", "50", 2)),
        trades_frame(trade("BTC", "101", 3)),
    ]
    seen, _ = _drive(frames, symbols=["BTC", "ETH"], until_ticks=3)

    seqs = {(t.symbol, t.seq) for t in seen}
    assert seqs == {("BTC", 0), ("BTC", 1), ("ETH", 0)}


def test_live_ticks_dedup_on_the_venue_trade_id() -> None:
    frames = [trades_frame(trade("BTC", "100", 900000000000001))]
    seen, _ = _drive(frames, symbols=["BTC"], until_ticks=1)

    # Live-form weak key (ADR-0027): {symbol}:{tid}, not the replay form.
    assert seen[0].event_id == "BTC:900000000000001"


def test_slow_consumer_gets_only_the_latest_tick_per_symbol_with_one_lagged_per_drop() -> None:
    """ADR-0023: under backpressure the feed conflates — newest tick per symbol
    wins, every dropped tick emits one ``feed.lagged`` — while the WS keeps
    draining. Here the first publish blocks; three more ticks arrive meanwhile
    (BTC 101 → superseded by BTC 102 → one drop; ETH 50 kept)."""

    async def main() -> tuple[list[MarketTick], list[EventDict]]:
        bus = InMemoryBus()
        clock = ManualClock()
        seen: list[MarketTick] = []
        release = asyncio.Event()
        first_delivered = asyncio.Event()

        async def slow_consumer(tick: MarketTick) -> None:
            seen.append(tick)
            if len(seen) == 1:
                first_delivered.set()
                await release.wait()

        bus.subscribe(MarketTick, slow_consumer)
        connection = FakeWsConnection(
            [
                trades_frame(trade("BTC", "100", 1)),
                trades_frame(trade("BTC", "101", 2)),
                trades_frame(trade("BTC", "102", 3)),
                trades_frame(trade("ETH", "50", 4)),
            ]
        )

        feed = HyperliquidFeed(
            config=HyperliquidConfig(symbols=["BTC", "ETH"]),
            bus=bus,
            clock=clock,
            connect=connection.connect,
        )
        with capture_events() as logs:
            await feed.start()
            run = asyncio.create_task(feed.run())
            await asyncio.wait_for(first_delivered.wait(), timeout=2)
            # The publish is stuck in the slow consumer; the reader must still
            # drain the socket to the end before we let the consumer go.
            await asyncio.wait_for(connection.drained.wait(), timeout=2)
            release.set()

            async def rest_published() -> None:
                while len(seen) < 3:
                    await asyncio.sleep(0)

            await asyncio.wait_for(rest_published(), timeout=2)
            await feed.stop()
            await asyncio.wait_for(run, timeout=2)
        return seen, [log for log in logs if log["event"] == "feed.lagged"]

    seen, lagged = asyncio.run(main())

    # Latest per symbol only: BTC 101 was superseded while the consumer stalled.
    assert [(t.symbol, t.price) for t in seen] == [
        ("BTC", Decimal("100")),
        ("BTC", Decimal("102")),
        ("ETH", Decimal("50")),
    ]
    # Exactly one drop, named per ADR-0020/0023, identifying the dropped tick.
    assert len(lagged) == 1
    assert lagged[0]["symbol"] == "BTC"
    assert lagged[0]["dropped_trade_id"] == "2"


def test_a_publish_that_raises_tears_the_socket_reader_down_with_it() -> None:
    """The fault twin of the stall above: a *slow* subscriber must not stop the
    reader draining the socket, and a *failing* one must stop it at once.

    The two coroutines behind one connection are paired in a ``TaskGroup`` for
    exactly this — a reader that outlived its publisher would keep pulling
    frames, conflating them into a buffer nothing drains again, and hold the
    socket open on an engine that is already faulting. So the fault leaves the
    group instead of being swallowed by it: ``WsSession`` answers only a refused
    *connect*, which is what makes the runner's supervising ``TaskGroup`` the
    fault channel for everything a consumer raises (ADR-0024).

    Distinct from ``test_session.py``'s consumer-raises case, which drives a
    stand-in ``consume`` and pins the session's no-reconnect policy. This one
    pins the pairing the real ``consume`` is built from, and nothing else
    reaches it: the parse path never raises (a bad frame is a named drop), so a
    failing publish is the only way this group is ever asked to abort.
    """

    class Boom(Exception):
        """A subscriber's fault, from the far side of ``bus.publish``."""

    async def main() -> FakeWsConnection:
        bus = InMemoryBus()

        async def explode(tick: MarketTick) -> None:
            raise Boom

        bus.subscribe(MarketTick, explode)
        # More frames than the reader can have read: the first tick faults the
        # publisher, so a reader still standing would consume the rest and set
        # ``drained``. ``drop_when_drained`` keeps that failure a failed
        # assertion rather than a hang on a socket nobody closes.
        connection = FakeWsConnection(
            [trades_frame(trade("BTC", "100", tid)) for tid in (1, 2, 3)],
            drop_when_drained=True,
        )

        feed = HyperliquidFeed(
            config=HyperliquidConfig(symbols=["BTC"]),
            bus=bus,
            clock=ManualClock(),
            connect=connection.connect,
        )
        await feed.start()
        with pytest.raises(ExceptionGroup) as raised:
            await asyncio.wait_for(feed.run(), timeout=2)

        # The subscriber's fault alone: the reader's cancellation is the group's
        # own doing and is not reported as a second failure.
        assert [type(error) for error in raised.value.exceptions] == [Boom]
        return connection

    connection = asyncio.run(main())

    assert not connection.drained.is_set()  # torn down mid-socket, not run to the end


def test_ws_drop_reconnects_with_backoff_resubscribes_and_resumes() -> None:
    """The venue hangs up after one tick, the next connect attempt is refused,
    the one after succeeds: the feed sleeps the doubling backoff on the injected
    clock (1s, then 2s — never a real sleep), resubscribes, and resumes."""

    async def main() -> tuple[list[MarketTick], FakeWsConnection, RecordingClock, int]:
        bus = InMemoryBus()
        clock = RecordingClock()
        seen: list[MarketTick] = []
        resumed = asyncio.Event()

        async def record(tick: MarketTick) -> None:
            seen.append(tick)
            if len(seen) >= 2:
                resumed.set()

        bus.subscribe(MarketTick, record)
        first = FakeWsConnection([trades_frame(trade("BTC", "100", 1))], drop_when_drained=True)
        second = FakeWsConnection([trades_frame(trade("BTC", "101", 2))])
        outcomes: list[FakeWsConnection | Exception] = [
            first,
            ConnectionRefusedError("venue hiccup"),
            second,
        ]
        connects = 0

        async def connect(url: str) -> FakeWsConnection:
            nonlocal connects
            connects += 1
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        feed = HyperliquidFeed(
            config=HyperliquidConfig(symbols=["BTC"]), bus=bus, clock=clock, connect=connect
        )
        await feed.start()
        run = asyncio.create_task(feed.run())
        await asyncio.wait_for(resumed.wait(), timeout=2)
        await feed.stop()
        await asyncio.wait_for(run, timeout=2)
        return seen, second, clock, connects

    seen, second, clock, connects = asyncio.run(main())

    assert [t.price for t in seen] == [Decimal("100"), Decimal("101")]  # resumed
    assert connects == 3
    assert clock.sleeps == [1.0, 2.0]  # doubling backoff, virtual time only
    assert json.loads(second.sent[0]) == {  # resubscribed on the new socket
        "method": "subscribe",
        "subscription": {"type": "trades", "coin": "BTC"},
    }


def test_stop_does_not_trigger_a_reconnect() -> None:
    connection = FakeWsConnection([trades_frame(trade("BTC", "100", 1))])
    connect_count = 0

    async def main() -> None:
        nonlocal connect_count
        bus = InMemoryBus()
        got_one = asyncio.Event()

        async def record(tick: MarketTick) -> None:
            got_one.set()

        bus.subscribe(MarketTick, record)

        async def connect(url: str) -> FakeWsConnection:
            nonlocal connect_count
            connect_count += 1
            return connection

        feed = HyperliquidFeed(
            config=HyperliquidConfig(symbols=["BTC"]), bus=bus, clock=ManualClock(), connect=connect
        )
        await feed.start()
        run = asyncio.create_task(feed.run())
        await asyncio.wait_for(got_one.wait(), timeout=2)
        await feed.stop()
        await asyncio.wait_for(run, timeout=2)

    asyncio.run(main())
    assert connect_count == 1


def _drive_marks(
    frames: list[str], *, symbols: list[str], until_marks: int
) -> tuple[list[MarkTick], FakeWsConnection]:
    """``_drive``'s mark twin: run until ``until_marks`` marks arrive, then stop.

    The clock is pinned rather than defaulted because unlike a tick's, a mark's
    ``ts_event`` **is** the clock read — the ``activeAssetCtx`` channel carries
    no instant of its own — so it is an assertable value here (ADR-0039).
    """
    return _run_feed(
        frames,
        symbols=symbols,
        event_type=MarkTick,
        until=until_marks,
        clock=ManualClock(start_ns=4_000),
    )


def test_the_active_asset_ctx_channel_yields_one_mark_per_update() -> None:
    """The live mark's ingress (ADR-0039): ``ctx.markPx``, and nothing else in
    that context.

    ``oraclePx`` is funding's price and ``midPx`` is the book's — both ride the
    same frame, and either would value a position against a number the venue
    does not margin it with. The channel carries no timestamp of its own, so
    ``ts_event`` is receipt time off the injected clock (ADR-0005/0039).
    """
    frames = [asset_ctx_frame("BTC", "43251.0")]
    seen, _ = _drive_marks(frames, symbols=["BTC"], until_marks=1)

    mark = seen[0]
    assert mark.symbol == "BTC"
    assert mark.price == Decimal("43251.0")
    assert mark.ts_event == 4_000
    # The live form of the weak key: no ``seq``, because receipt time already
    # separates two marks on a channel that updates every few seconds.
    assert mark.event_id == "BTC:4000"


def test_the_mark_channel_is_subscribed_per_symbol_beside_the_trades_one() -> None:
    """Both channels, per coin, on one connection — and both **unauthenticated**,
    so the live feed still needs no key at all (ADR-0021)."""
    frames = [asset_ctx_frame("BTC", "100")]
    _, connection = _drive_marks(frames, symbols=["BTC", "ETH"], until_marks=1)

    assert [json.loads(m) for m in connection.sent] == [
        {"method": "subscribe", "subscription": {"type": "trades", "coin": "BTC"}},
        {"method": "subscribe", "subscription": {"type": "activeAssetCtx", "coin": "BTC"}},
        {"method": "subscribe", "subscription": {"type": "trades", "coin": "ETH"}},
        {"method": "subscribe", "subscription": {"type": "activeAssetCtx", "coin": "ETH"}},
    ]


def test_mark_prices_parse_to_decimal_never_float() -> None:
    frames = [asset_ctx_frame("BTC", "0.1")]
    seen, _ = _drive_marks(frames, symbols=["BTC"], until_marks=1)

    assert isinstance(seen[0].price, Decimal)
    assert seen[0].price == Decimal("0.1")  # exact — a float round-trip would not be


@pytest.mark.parametrize("figure", ["NaN", "Infinity", "-Infinity"])
def test_a_non_finite_mark_is_dropped_rather_than_valuing_a_position(figure: str) -> None:
    """The same refusal the tick figures get, and it matters more here: a
    ``NaN`` mark would not merely mis-publish, it would propagate into every
    Tier-2 number the projection recomputes from it and surface as an
    ``InvalidOperation`` from some comparison far downstream."""
    frames = [asset_ctx_frame("BTC", figure), asset_ctx_frame("BTC", "100")]
    with capture_events() as logs:
        seen, _ = _drive_marks(frames, symbols=["BTC"], until_marks=1)

    assert [m.price for m in seen] == [Decimal("100")]
    assert [record["event"] for record in logs] == ["feed.frame_dropped"]


@pytest.mark.parametrize(
    "data",
    [
        pytest.param({"coin": "BTC", "ctx": {"oraclePx": "1"}}, id="ctx-without-markPx"),
        pytest.param({"coin": "BTC"}, id="no-ctx-at-all"),
        pytest.param({"coin": "BTC", "ctx": "43251.0"}, id="ctx-not-an-object"),
        pytest.param(["BTC", {"markPx": "1"}], id="data-not-an-object"),
        pytest.param(None, id="data-null"),
    ],
)
def test_a_mark_frame_we_cannot_read_is_dropped_and_named_not_faulted(data: object) -> None:
    """Every shape the mark could arrive malformed in answers the same way: one
    ``feed.frame_dropped`` and a skip, never a fault and never a mark.

    The live stream is lossy by contract (ADR-0023), and a dead mark surfaces as
    Tier-2 divergence on the reconcile cycle — so refusing one frame costs a
    valuation refresh, where faulting the feed would cost the run. The next good
    frame still flows, which is what makes that trade-off honest.
    """
    frames = [
        json.dumps({"channel": "activeAssetCtx", "data": data}),
        asset_ctx_frame("BTC", "100"),
    ]
    with capture_events() as logs:
        seen, _ = _drive_marks(frames, symbols=["BTC"], until_marks=1)

    assert [m.price for m in seen] == [Decimal("100")]
    assert [record["event"] for record in logs] == ["feed.frame_dropped"]


def test_the_live_feed_connects_in_start_and_leaves_the_loop_to_run() -> None:
    """The lifecycle half of the seam, now shaped like ``Exchange``'s (#226).

    ``start()`` opens and subscribes the socket and **returns**; ``run()`` is the
    long-lived half the runner supervises. The bound on ``start()`` is the
    assertion rather than a guard against a slow test: a ``start()`` that *is*
    the loop never returns at all, which is precisely the defect — there was no
    instant at which the runner could fail a boot on an unreachable feed, so an
    engine could reach ``RUNNING`` with a feed that had never connected.

    Nothing is read off the socket until ``run()``, so ADR-0024's ordering is
    untouched: the connect is the last thing before the supervised task, not a
    socket left buffering across the barrier.
    """

    async def main() -> None:
        bus = InMemoryBus()
        transcript = record_market_data(bus)
        connection = FakeWsConnection([trades_frame(trade("BTC", "43000", 1))])
        connects = 0

        async def connect(url: str) -> FakeWsConnection:
            nonlocal connects
            connects += 1
            return connection

        feed = HyperliquidFeed(
            config=HyperliquidConfig(symbols=["BTC"]),
            bus=bus,
            clock=ManualClock(),
            connect=connect,
        )

        await asyncio.wait_for(feed.start(), timeout=2)

        assert connects == 1
        assert connection.sent, "start() must subscribe the socket it opened"
        assert transcript.ticks == [], "start() must not consume — that is run()'s"

        run = asyncio.create_task(feed.run())
        await asyncio.wait_for(connection.drained.wait(), timeout=2)
        await feed.stop()
        await asyncio.wait_for(run, timeout=2)

        assert [t.symbol for t in transcript.ticks] == ["BTC"]
        assert connects == 1, "run() must consume the socket start() opened, not open a second"

    asyncio.run(main())


def test_a_first_connect_the_venue_refuses_faults_the_boot_rather_than_backing_off() -> None:
    """The point of the triple, stated as the failure it buys (#227).

    A refused connect is a *reconnect's* business inside ``run()``, where pacing
    it and going round again is exactly right — an outage mid-run must not
    become a retry storm, and must not end the engine. At boot it is the
    opposite fact: nothing has arrived yet, nothing can, and the operator wants
    to know now. So ``start()`` refuses rather than paces, and the runner's
    inline ``await`` at ADR-0024 step 7 turns that into a faulted boot.

    Two independent witnesses that the boot did not enter the reconnect loop,
    neither of which is a timeout: **one** connect was attempted, and virtual
    time never moved. ``Backoff.sleep_on`` advances a ``ManualClock``, so a
    ``start()`` that paced even one retry would leave the clock past zero.
    """

    async def main() -> None:
        clock = ManualClock()
        connects = 0

        async def connect(url: str) -> FakeWsConnection:
            nonlocal connects
            connects += 1
            raise ConnectionRefusedError("connection refused")

        feed = HyperliquidFeed(
            config=HyperliquidConfig(symbols=["BTC"]),
            bus=InMemoryBus(),
            clock=clock,
            connect=connect,
        )

        with pytest.raises(ConnectionRefusedError):
            await feed.start()

        assert connects == 1, "start() must refuse the first connect, not retry it"
        assert clock.timestamp_ns() == 0, "a paced retry would have moved virtual time"

        # The boot's own cleanup still runs on the fault path (`_stop_feed`), and
        # a session that never opened a socket has nothing to close.
        await feed.stop()

    asyncio.run(main())


def _drive_contract(
    frames: list[str], *, symbols: list[str], expected: int
) -> MarketDataTranscript:
    """Run the feed over ``frames`` until both streams have landed, then stop.

    ``_run_feed`` above waits on a count of **one** event type, which cannot
    express this: the obligation relates the two streams, so a driver that
    stopped at the last trade would race the mark behind it and a driver that
    stopped at the last mark would race the trade.

    ``expected`` is the total across both streams, and a timeout waiting for it
    is **suppressed rather than raised**. That is the load-bearing line. The
    wait is an optimisation — it ends the run as soon as the venue's frames are
    through instead of parking for the full timeout — but a feed that published
    no mark would never reach the count, and failing here would report a
    ``TimeoutError`` from a test helper for what is a contract violation with a
    sentence of its own. Falling through hands the verdict to the contract.
    """

    async def main() -> MarketDataTranscript:
        bus = InMemoryBus()
        transcript = record_market_data(bus)
        enough = asyncio.Event()

        async def count(_event: MarketTick | MarkTick) -> None:
            if len(transcript.ticks) + len(transcript.marks) >= expected:
                enough.set()

        bus.subscribe(MarketTick, count)
        bus.subscribe(MarkTick, count)
        connection = FakeWsConnection(frames)

        feed = HyperliquidFeed(
            config=HyperliquidConfig(symbols=symbols),
            bus=bus,
            clock=ManualClock(start_ns=4_000),
            connect=connection.connect,
        )
        await feed.start()
        run = asyncio.create_task(feed.run())
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(enough.wait(), timeout=2)
        await feed.stop()
        await asyncio.wait_for(run, timeout=2)
        return transcript

    return asyncio.run(main())


def test_the_live_feed_marks_every_symbol_it_trades() -> None:
    """The shared ``MarketFeed`` obligation (``tests/_support/feed_contract.py``),
    driven over recorded frames from the socket this adapter opens.

    Word for word the assertion ``ReplayFeed`` answers, which is the point: two
    adapters that reach the mark by entirely different routes — a proxy derived
    from the trade price, versus ``ctx.markPx`` off a second subscribed channel
    — owe the engine the same thing, and a third venue learns the obligation
    from a contract rather than from prose in a checklist.

    The frames interleave the two channels per symbol because that is what the
    venue does; nothing here asserts an order between them, since the streams
    are independent and only the live adapter's own tests above pin how each is
    read.
    """
    frames = [
        trades_frame(trade("BTC", "43000", 1)),
        asset_ctx_frame("BTC", "43251.0"),
        trades_frame(trade("ETH", "2200", 2)),
        asset_ctx_frame("ETH", "2205.0"),
    ]

    transcript = _drive_contract(frames, symbols=["BTC", "ETH"], expected=4)

    assert_every_traded_symbol_is_marked(transcript, feed="HyperliquidFeed")


def test_non_trades_frames_are_ignored_silently() -> None:
    """The venue's housekeeping traffic is skipped and says nothing about it —
    the *silence* is the assertion, not a side effect of one.

    A ``subscriptionResponse`` or a ``pong`` is the venue working, and it arrives
    for as long as the socket lives; naming one would drown every real drop in
    the same log. That is the opposite answer to the malformed frames below, and
    the two are only kept apart by both being pinned: without the empty-log line
    here, a change that named housekeeping passes this case and the one below it
    alike, and the distinction the pair exists to hold survives in prose only.
    """
    frames = [
        json.dumps({"channel": "subscriptionResponse", "data": {"method": "subscribe"}}),
        json.dumps({"channel": "pong"}),
        trades_frame(trade("BTC", "100", 1)),
    ]
    with capture_events() as logs:
        seen, _ = _drive(frames, symbols=["BTC"], until_ticks=1)

    assert [t.trade_id for t in seen] == ["1"]
    assert [record["event"] for record in logs] == []


@pytest.mark.parametrize("frame", ["[1,2]", '"hello"', "42", "null"])
def test_a_frame_that_is_json_but_not_an_object_is_dropped_and_named(frame: str) -> None:
    """A frame the feed cannot read is named however it is malformed — deliberately
    the *opposite* answer to the unsourced-channel frames pinned directly above.

    The two look alike and are not: a ``subscriptionResponse`` is the venue
    working, constant housekeeping traffic that would drown the log if named,
    while a bare list or a naked ``null`` is the venue breaking its own contract.
    Filing the second under the first's silence is what makes a total feed loss
    indistinguishable from a quiet market — no named event, no exception, no
    reconnect, while every Tier-2 valuation decays to ``None`` (ADR-0039) and
    nothing fills. ADR-0023 makes the stream lossy by contract; ADR-0020's
    catalog is what keeps the loss observable, so it is dropped, never silently.

    Parametrized over all four non-object JSON kinds because ``null`` in
    particular reads as an absence rather than a corruption, and is the one most
    likely to be special-cased back into silence.
    """
    frames = [frame, trades_frame(trade("BTC", "100", 1))]
    with capture_events() as logs:
        seen, _ = _drive(frames, symbols=["BTC"], until_ticks=1)

    assert [t.trade_id for t in seen] == ["1"]  # the good frame after it still ticks
    assert [record["event"] for record in logs] == ["feed.frame_dropped"]


@pytest.mark.parametrize("figure", ["NaN", "Infinity", "-Infinity"])
def test_a_non_finite_tick_figure_is_dropped_not_ticked(figure: str) -> None:
    """``Decimal("NaN")``/``Decimal("Infinity")`` are *valid* constructions, so a
    non-finite ``px``/``sz`` is the one unreadable venue figure that raises
    nothing on the way in. It must be a dropped row like any other: a ``NaN``
    tick does not clear a downstream band check quietly — ``Decimal`` ordering
    *signals* on it — it detonates one layers away from the read that admitted
    it, and its equality and arithmetic are silently wrong all the way there."""

    async def main() -> tuple[list[MarketTick], list[EventDict]]:
        bus = InMemoryBus()
        clock = ManualClock()
        seen: list[MarketTick] = []
        enough = asyncio.Event()

        async def record(tick: MarketTick) -> None:
            seen.append(tick)
            enough.set()

        bus.subscribe(MarketTick, record)
        connection = FakeWsConnection(
            [
                trades_frame(trade("BTC", figure, 1)),  # non-finite price
                trades_frame(trade("BTC", "100", 2, sz=figure)),  # non-finite size
                trades_frame(trade("BTC", "100", 3)),  # the sentinel that must arrive
            ]
        )

        feed = HyperliquidFeed(
            config=HyperliquidConfig(symbols=["BTC"]),
            bus=bus,
            clock=clock,
            connect=connection.connect,
        )
        with capture_events() as logs:
            await feed.start()
            run = asyncio.create_task(feed.run())
            await asyncio.wait_for(enough.wait(), timeout=2)
            await feed.stop()
            await asyncio.wait_for(run, timeout=2)
        return seen, [log for log in logs if log["event"] == "feed.frame_dropped"]

    seen, dropped = asyncio.run(main())

    # Only the finite trade reached the bus; neither non-finite row became a tick.
    assert [t.trade_id for t in seen] == ["3"]
    assert len(dropped) == 2


@pytest.mark.parametrize("figure", [100.5, 100, 1e30, 43250.123456789012345])
def test_a_re_typed_tick_figure_is_dropped_not_coerced(figure: object) -> None:
    """A ``px``/``sz`` the venue re-types as a JSON *number* is a dropped row.

    The venue reports both as decimal strings — first-party ``WsTrade`` is
    ``px: string, sz: string`` — so a number is the venue changing its contract,
    the same "we are not reading what we think we are" a missing field means. It
    cannot be coerced through either, and not because of our own parse —
    ``Decimal(str(x))`` would round-trip ``0.002`` exactly. ``json.loads`` is
    where it goes: a JSON number is a ``float`` before this reader sees it, so a
    reported ``43250.123456789012345`` arrives as ``43250.12345678901`` and a
    reported ``0.10`` is indistinguishable from ``0.1``. Neither is recoverable
    downstream, so the tick is no longer the exact figure ADR-0029 builds every
    price on — durable once a fill computed against it is written.

    The account grain has frozen on this since #217; the tick grain waved it
    through, which is the divergence this closes: one venue, one contract.
    """

    async def main() -> tuple[list[MarketTick], list[EventDict]]:
        bus = InMemoryBus()
        clock = ManualClock()
        seen: list[MarketTick] = []
        enough = asyncio.Event()

        async def record(tick: MarketTick) -> None:
            seen.append(tick)
            enough.set()

        bus.subscribe(MarketTick, record)
        connection = FakeWsConnection(
            [
                trades_frame(trade("BTC", figure, 1)),  # re-typed price
                trades_frame(trade("BTC", "100", 2, sz=figure)),  # re-typed size
                trades_frame(trade("BTC", "100", 3)),  # the sentinel that must arrive
            ]
        )

        feed = HyperliquidFeed(
            config=HyperliquidConfig(symbols=["BTC"]),
            bus=bus,
            clock=clock,
            connect=connection.connect,
        )
        with capture_events() as logs:
            await feed.start()
            run = asyncio.create_task(feed.run())
            await asyncio.wait_for(enough.wait(), timeout=2)
            await feed.stop()
            await asyncio.wait_for(run, timeout=2)
        return seen, [log for log in logs if log["event"] == "feed.frame_dropped"]

    seen, dropped = asyncio.run(main())

    assert [t.trade_id for t in seen] == ["3"]
    assert len(dropped) == 2


def test_malformed_frames_are_skipped_and_named_while_good_frames_keep_flowing() -> None:
    """A corrupt frame or trade row must never fault the feed (ADR-0023): each
    emits one ``feed.frame_dropped`` and is skipped, and good rows — even in the
    same batch as a bad one — still tick through."""

    async def main() -> tuple[list[MarketTick], list[EventDict]]:
        bus = InMemoryBus()
        clock = ManualClock()
        seen: list[MarketTick] = []
        enough = asyncio.Event()

        async def record(tick: MarketTick) -> None:
            seen.append(tick)
            if len(seen) >= 2:
                enough.set()

        bus.subscribe(MarketTick, record)
        connection = FakeWsConnection(
            [
                "}not json{",  # frame-level garbage → one drop
                json.dumps({"channel": "trades", "data": "oops"}),  # trades frame, data not a list
                trades_frame({"coin": "BTC", "side": "B", "px": "nope"}),  # unparseable row
                trades_frame(trade("BTC", "100", 1), {"coin": "BTC"}),  # one good, one bad row
                trades_frame(trade("BTC", "101", 2), trade("BTC", "NaN", 9)),  # good + non-finite
            ]
        )

        feed = HyperliquidFeed(
            config=HyperliquidConfig(symbols=["BTC"]),
            bus=bus,
            clock=clock,
            connect=connection.connect,
        )
        with capture_events() as logs:
            await feed.start()
            run = asyncio.create_task(feed.run())
            await asyncio.wait_for(enough.wait(), timeout=2)
            await feed.stop()
            await asyncio.wait_for(run, timeout=2)
        return seen, [log for log in logs if log["event"] == "feed.frame_dropped"]

    seen, dropped = asyncio.run(main())

    # Both good trades ticked through despite the garbage around and beside them.
    assert [t.trade_id for t in seen] == ["1", "2"]
    # One drop each: the non-JSON frame, the non-list `data`, the bad-only row,
    # the bad row in the mixed batch, and the non-finite row beside trade 2 —
    # a figure that is not a number drops at row grain like any other, so the
    # good trade batched with it is unaffected.
    assert len(dropped) == 5


def test_the_live_feed_satisfies_the_market_feed_seam() -> None:
    """Conformance asserted at the adapter, as the replay suite asserts its own
    and both ``Exchange`` adapters assert theirs. ``MarketFeed`` is
    ``runtime_checkable``, so this is a member-presence check; the half it cannot
    see — a member implemented but unasserted — is ``_SEAM_CLAIMS``' below."""
    feed = HyperliquidFeed(
        config=HyperliquidConfig(symbols=["BTC"]), bus=InMemoryBus(), clock=ManualClock()
    )

    assert isinstance(feed, MarketFeed)


# Which test claims each ``MarketFeed`` member for *this* adapter. Not a second
# copy of the seam: the gate below asserts it against the Protocol itself, so a
# new member cannot arrive without someone naming what asserts it here.
_SEAM_CLAIMS = {
    "start": "test_the_live_feed_connects_in_start_and_leaves_the_loop_to_run",
    "run": "test_ws_drop_reconnects_with_backoff_resubscribes_and_resumes",
    "stop": "test_stop_does_not_trigger_a_reconnect",
}


def test_every_market_feed_member_carries_a_claim_in_the_live_suite() -> None:
    """The completeness gate the ``isinstance`` check above cannot be (#227).

    Deliberately the same three members answered by a different three tests than
    the replay suite names: the seam is one obligation and the adapters meet it
    in their own idioms, which is why the claim is declared per suite rather than
    driven from one shared map the way ``Store``'s identical-behaviour gate is.
    ``run`` is claimed by the reconnect test rather than by any of the parsing
    ones above it — those exercise the loop incidentally, while that one asserts
    what makes it the long-lived half: it survives its sockets."""
    assert_every_member_is_claimed(MarketFeed, _SEAM_CLAIMS, suite=Path(__file__).parent)
