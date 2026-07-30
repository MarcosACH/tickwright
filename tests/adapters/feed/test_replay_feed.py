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

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.feed import ReplayFeed
from tickwright.domain import MarketTick


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
    asyncio.run(feed.start())
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
    first bad row raises out of ``start()``, so a second one below it would never
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
        asyncio.run(feed.start())

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
