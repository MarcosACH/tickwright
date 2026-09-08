"""``ReplayFeed`` — file-backed, deterministic, never-conflating market feed.

For each JSONL row the feed advances the injected ``ManualClock`` to the row's
``ts_event`` and publishes a ``MarketTick`` (ADR-0027). Prices and sizes parse
straight to ``Decimal`` (never float, ADR-0029). Replay stays faithful: every row
is delivered in file order, with no conflation (ADR-0023).
"""

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import pytest
from feed_contract import assert_every_traded_symbol_is_marked, record_market_data
from seam_claims import assert_every_member_is_claimed

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.feed import ReplayFeed
from tickwright.domain import Event, MarketFeed, MarketTick, MarkTick


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def _row(symbol: str, price: str, ts_event: int, trade_id: str, size: str = "1") -> dict:
    return {
        "symbol": symbol,
        "price": price,
        "size": size,
        "aggressor_side": "buy",
        "trade_id": trade_id,
        "ts_event": ts_event,
    }


def _collect(path: Path) -> tuple[list[MarketTick], ManualClock]:
    bus = InMemoryBus()
    clock = ManualClock()
    seen: list[MarketTick] = []

    async def record(tick: MarketTick) -> None:
        seen.append(tick)

    bus.subscribe(MarketTick, record)
    feed = ReplayFeed(path=path, bus=bus, clock=clock)
    asyncio.run(feed.run())
    return seen, clock


@pytest.mark.parametrize("field", ["price", "size"])
@pytest.mark.parametrize("figure", ["NaN", "Infinity", "-Infinity"])
def test_replay_refuses_a_non_finite_figure_rather_than_publishing_it(
    tmp_path: Path, figure: str, field: str
) -> None:
    """A recorded file is not a venue, but it is the same parse, and a
    ``Decimal("NaN")`` is just as valid a construction here. A figure that is not
    a number is an unreadable row, which replay has always refused by raising —
    what must never happen is that it publishes as a tick, because the failure
    then surfaces as an ``InvalidOperation`` from whichever guard downstream
    compared against it, blaming a layer that read the tick correctly.

    ``field`` is parametrized rather than putting both figures in one file: the
    first bad row raises out of ``run()``, so a second one below it would never
    be parsed at all and the guard on that field would go unexercised — invisibly,
    since the other rows keep its line covered."""
    bad = _row("BTC", "100", 2_000, "b")
    bad[field] = figure
    path = _write_jsonl(
        tmp_path / "ticks.jsonl",
        [
            _row("BTC", "100", 1_000, "a"),
            bad,
            _row("BTC", "102", 3_000, "c"),
        ],
    )
    bus = InMemoryBus()
    seen: list[MarketTick] = []

    async def record(tick: MarketTick) -> None:
        seen.append(tick)

    bus.subscribe(MarketTick, record)
    feed = ReplayFeed(path=path, bus=bus, clock=ManualClock())

    with pytest.raises(ValueError):
        asyncio.run(feed.run())

    # The good row ahead of it published; the non-finite one never did, and
    # nothing behind it was reached — a raise is replay's whole answer.
    assert [t.trade_id for t in seen] == ["a"]


def test_replay_publishes_every_tick_in_file_order(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path / "ticks.jsonl",
        [
            _row("BTC", "100", 1_000, "a"),
            _row("BTC", "101", 2_000, "b"),
            _row("BTC", "102", 3_000, "c"),
        ],
    )
    seen, _ = _collect(path)

    assert [t.trade_id for t in seen] == ["a", "b", "c"]
    assert [t.price for t in seen] == [Decimal("100"), Decimal("101"), Decimal("102")]


def test_replay_never_conflates_same_symbol_ticks(tmp_path: Path) -> None:
    # Three ticks for one symbol: all three are delivered, not just the latest.
    path = _write_jsonl(
        tmp_path / "ticks.jsonl",
        [
            _row("BTC", "100", 1_000, "a"),
            _row("BTC", "101", 2_000, "b"),
            _row("BTC", "102", 3_000, "c"),
        ],
    )
    seen, _ = _collect(path)
    assert len(seen) == 3


def test_replay_derives_one_mark_per_row_ahead_of_the_trade_it_came_from(
    tmp_path: Path,
) -> None:
    """Replay is a paper deployment, so its mark is the last-trade proxy derived
    per row — no new column, because the file is trades-only (ADR-0039).

    **Ahead of** the ``MarketTick`` deliberately: the mark a trade implies is
    known as of that trade, so a handler reacting to the trade already holds it.
    Published behind, a fill on the first row would value at a mark that had not
    arrived yet and read ``None`` for one cascade.
    """
    path = _write_jsonl(
        tmp_path / "ticks.jsonl",
        [_row("BTC", "100", 1_000, "a"), _row("BTC", "101", 2_000, "b")],
    )
    bus = InMemoryBus()
    seen: list[Event] = []

    async def record(event: Event) -> None:
        seen.append(event)

    bus.subscribe(Event, record)
    asyncio.run(ReplayFeed(path=path, bus=bus, clock=ManualClock()).run())

    assert [(type(e).__name__, str(getattr(e, "price", ""))) for e in seen] == [
        ("MarkTick", "100"),
        ("MarketTick", "100"),
        ("MarkTick", "101"),
        ("MarketTick", "101"),
    ]


def test_replay_never_conflates_the_marks_it_derives(tmp_path: Path) -> None:
    """One mark per row, three rows, three marks — replay must stay faithful
    (ADR-0023), and a derived event is no more droppable than a read one."""
    path = _write_jsonl(
        tmp_path / "ticks.jsonl",
        [
            _row("BTC", "100", 1_000, "a"),
            _row("BTC", "101", 2_000, "b"),
            _row("BTC", "102", 3_000, "c"),
        ],
    )
    bus = InMemoryBus()
    marks: list[MarkTick] = []

    async def record(mark: MarkTick) -> None:
        marks.append(mark)

    bus.subscribe(MarkTick, record)
    asyncio.run(ReplayFeed(path=path, bus=bus, clock=ManualClock()).run())

    assert [m.price for m in marks] == [Decimal("100"), Decimal("101"), Decimal("102")]
    # Each carries the row's own instant and the per-symbol seq that keeps two
    # marks at one instant apart, so the derived stream is as replayable as the
    # trades it came from.
    assert [m.event_id for m in marks] == ["BTC:1000:0", "BTC:2000:1", "BTC:3000:2"]


def test_replay_holds_its_rows_until_run(tmp_path: Path) -> None:
    """The same lifecycle the live feed takes, on the adapter with nothing to
    connect (#226/#227).

    A replay has no socket to open, so ``start()`` is a no-op here — and that is
    the point rather than an omission: a venue author reads one lifecycle shape
    across both seams and both adapters, and the runner drives one sequence
    without asking which feed it has. What must hold is that no row escapes
    before the supervised half runs, so the file is not drained by a step the
    runner awaits inline ahead of the barrier's own ordering.
    """
    path = _write_jsonl(
        tmp_path / "ticks.jsonl",
        [_row("BTC", "100", 1_000, "a"), _row("BTC", "101", 2_000, "b")],
    )

    async def main() -> None:
        bus = InMemoryBus()
        transcript = record_market_data(bus)
        feed = ReplayFeed(path=path, bus=bus, clock=ManualClock())

        await feed.start()
        assert transcript.ticks == [], "start() must not replay the file — that is run()'s"

        await feed.run()
        assert [t.price for t in transcript.ticks] == [Decimal("100"), Decimal("101")]

    asyncio.run(main())


def test_replay_marks_every_symbol_it_trades(tmp_path: Path) -> None:
    """The shared ``MarketFeed`` obligation (``tests/_support/feed_contract.py``),
    driven over the finite file this adapter reads.

    The assertions above already state *how* replay derives its mark — the
    last-trade proxy, one per row, never conflated. This one states the thing
    the live adapter states too, in the same words, so the obligation is one
    sentence with two subjects rather than two suites that happen to agree.

    Multi-symbol on purpose: the omission this catches is per-symbol, and a
    single-symbol file cannot tell "marks everything it trades" from "marks the
    one symbol anybody tested".
    """
    path = _write_jsonl(
        tmp_path / "ticks.jsonl",
        [
            _row("BTC", "100", 1_000, "a"),
            _row("ETH", "50", 1_500, "x"),
            _row("SOL", "20", 2_000, "s"),
            _row("BTC", "101", 2_500, "b"),
        ],
    )
    bus = InMemoryBus()
    transcript = record_market_data(bus)

    asyncio.run(ReplayFeed(path=path, bus=bus, clock=ManualClock()).run())

    assert_every_traded_symbol_is_marked(transcript, feed="ReplayFeed")


def test_replay_advances_the_clock_to_each_tick_ts_event(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path / "ticks.jsonl",
        [_row("BTC", "100", 1_000, "a"), _row("BTC", "101", 5_000, "b")],
    )
    seen, clock = _collect(path)

    # ts_init is stamped from the clock *after* it is advanced, so it equals
    # ts_event on the replay path — the whole run is deterministic in time.
    assert [t.ts_event for t in seen] == [1_000, 5_000]
    assert [t.ts_init for t in seen] == [1_000, 5_000]
    assert clock.timestamp_ns() == 5_000  # left at the last tick's time


def test_replay_prices_are_decimal_not_float(tmp_path: Path) -> None:
    path = _write_jsonl(tmp_path / "ticks.jsonl", [_row("BTC", "0.1", 1_000, "a", size="0.2")])
    seen, _ = _collect(path)
    assert seen[0].price == Decimal("0.1")
    assert isinstance(seen[0].price, Decimal)
    assert isinstance(seen[0].size, Decimal)


def test_replay_assigns_per_symbol_source_sequence(tmp_path: Path) -> None:
    # seq disambiguates the weak replay dedup key {symbol}:{ts_event}:{seq}.
    path = _write_jsonl(
        tmp_path / "ticks.jsonl",
        [
            _row("BTC", "100", 1_000, "a"),
            _row("ETH", "50", 1_500, "x"),
            _row("BTC", "101", 2_000, "b"),
        ],
    )
    seen, _ = _collect(path)
    by_symbol: dict[str, list[int]] = {t.symbol: [] for t in seen}
    for t in seen:
        by_symbol[t.symbol].append(t.seq)
    assert by_symbol["BTC"] == [0, 1]
    assert by_symbol["ETH"] == [0]
    assert seen[0].event_id == "BTC:1000:0"


def test_replay_releases_nothing_and_never_interrupts_a_drain(tmp_path: Path) -> None:
    """The third member's claim on the adapter with nothing to release (#227).

    ``MarketFeed.stop`` promises idempotence and safety on a feed that never
    started, which a file-backed replay meets by holding nothing at all — asked
    twice, before any run, it does not raise.

    The second half is the one worth pinning, because it is a *difference* from
    the live adapter rather than an emptiness: replay's ``stop()`` is **not** a
    cooperative signal. A stopped ``WsSession`` refuses to reconnect and closes
    the socket its reader is blocked on; a stopped replay arms nothing, so a
    later ``run()`` drains the whole file regardless. That is not an oversight
    to fix here — it is why ``Engine._stop_feed`` cancels the supervised task
    rather than trusting the ask, and a replay that quietly grew a stop flag
    would make the runner's cancel look redundant while the two disagreed about
    which rows were delivered.
    """
    path = _write_jsonl(
        tmp_path / "ticks.jsonl",
        [_row("BTC", "100", 1_000, "a"), _row("BTC", "101", 2_000, "b")],
    )

    async def main() -> None:
        bus = InMemoryBus()
        transcript = record_market_data(bus)
        feed = ReplayFeed(path=path, bus=bus, clock=ManualClock())

        await feed.stop()
        await feed.stop()

        await feed.run()
        assert [t.price for t in transcript.ticks] == [Decimal("100"), Decimal("101")], (
            "replay holds no live resource, so a stop cannot arm one — the runner cancels"
        )

    asyncio.run(main())


def test_the_replay_feed_satisfies_the_market_feed_seam(tmp_path: Path) -> None:
    """Conformance asserted at the adapter, as both ``Exchange`` adapters assert
    theirs. ``MarketFeed`` is ``runtime_checkable``, so this is a member-presence
    check: a member added to the Protocol fails here for whichever adapter was
    left behind, with nobody maintaining a transcribed list of the seam. The half
    it cannot see — a member every adapter implements but no test asserts — is
    what ``_SEAM_CLAIMS`` below covers."""
    feed = ReplayFeed(path=tmp_path / "ticks.jsonl", bus=InMemoryBus(), clock=ManualClock())

    assert isinstance(feed, MarketFeed)


# Which test claims each ``MarketFeed`` member for *this* adapter. Not a second
# copy of the seam: the gate below asserts it against the Protocol itself, so a
# new member cannot arrive without someone naming what asserts it here.
_SEAM_CLAIMS = {
    "start": "test_replay_holds_its_rows_until_run",
    "run": "test_replay_publishes_every_tick_in_file_order",
    "stop": "test_replay_releases_nothing_and_never_interrupts_a_drain",
}


def test_every_market_feed_member_carries_a_claim_in_the_replay_suite() -> None:
    """The completeness gate the ``isinstance`` check above cannot be (#227).

    The seam just grew from two members to three, which is exactly the arrival
    this gate exists for: every adapter had to implement ``run()`` for the engine
    to boot at all, so conformance went green the moment it existed, while what
    each member *does* on this adapter was nobody's to notice. The live suite
    answers the same gate in its own idiom."""
    assert_every_member_is_claimed(MarketFeed, _SEAM_CLAIMS, suite=Path(__file__).parent)
