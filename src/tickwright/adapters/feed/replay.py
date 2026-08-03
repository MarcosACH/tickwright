"""``ReplayFeed`` — the hermetic, non-venue ``MarketFeed`` (ADR-0027).

Reads newline-delimited JSON (one ``MarketTick`` per line — readable, diffable)
and, for each row, advances the injected ``ReplayClock`` to the row's ``ts_event``
before publishing. Replay is therefore deterministic in time and never conflates
(ADR-0023): every recorded tick is delivered in file order. The tracer E2E and
every strategy test stand on this feed.

Each row publishes **two** events: the ``MarkTick`` the trade price implies
(ADR-0039's last-trade proxy — replay is a paper deployment, so no row schema
changes) and then the ``MarketTick`` itself.
"""

import asyncio
import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import TypedDict

from tickwright.domain import (
    AggressorSide,
    EventBus,
    MarketTick,
    MarkTick,
    ReplayClock,
    exact_figure,
)


class _TickRow(TypedDict):
    """The JSONL row schema a ``ReplayFeed`` reads (one last-trade tick per line)."""

    symbol: str
    price: str
    size: str
    aggressor_side: str
    trade_id: str
    ts_event: int


class ReplayFeed:
    """A file-backed ``MarketFeed`` that replays recorded ticks deterministically."""

    def __init__(self, *, path: Path, bus: EventBus, clock: ReplayClock) -> None:
        self._path = path
        self._bus = bus
        self._clock = clock
        # Per-symbol source sequence, disambiguating the weak replay dedup key.
        self._seq_by_symbol: dict[str, int] = {}

    async def start(self) -> None:
        for row in self._read_rows():
            # ``_to_tick`` advances virtual time, which may release parked
            # ``sleep_until`` waiters (ADR-0033). Yield once so a matured
            # cadence runs *at* its crossed deadline — before this row, whose
            # ``ts_event`` is at or past it, is published. Without the yield
            # nothing on the hermetic path ever suspends, and a woken waiter
            # would starve until end-of-file.
            tick = self._to_tick(row)
            await asyncio.sleep(0)
            # The mark goes **ahead of** the trade it was derived from, not
            # behind it: the two describe one instant, and a handler reacting to
            # the trade — a strategy placing, the venue filling, the projection
            # valuing what that fill produced — must already hold the mark that
            # instant implies. Behind it, the first row's fill would value
            # against a mark that had not arrived and read ``None`` for one
            # cascade, curing only on the next row.
            await self._bus.publish(self._to_mark(tick))
            await self._bus.publish(tick)

    async def stop(self) -> None:  # noqa: B027 - replay has no live resources to release.
        """No-op: a replay drains at ``start`` and holds nothing open."""

    def _read_rows(self) -> Iterator[_TickRow]:
        with self._path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    yield json.loads(line)

    @staticmethod
    def _to_mark(tick: MarketTick) -> MarkTick:
        """The last-trade proxy, derived per row (ADR-0039).

        Replay is a **paper deployment** reading the same trades-only file, so
        the mark is the trade price and the row schema gains nothing — there is
        no separate mark channel in a recording of trades. Deriving it here
        rather than letting the projection read ``MarketTick`` on paper is what
        keeps one uniform ``MarkTick`` stream on every deployment: provenance
        differs at the adapter, compute downstream does not.

        It inherits the tick's own instants and ``seq``, so the derived stream is
        exactly as replayable as the trades it came from — same file, same
        order, same ids, run after run.
        """
        return MarkTick(
            ts_event=tick.ts_event,
            ts_init=tick.ts_init,
            symbol=tick.symbol,
            price=tick.price,
            seq=tick.seq,
        )

    def _to_tick(self, row: _TickRow) -> MarketTick:
        symbol = row["symbol"]
        ts_event = int(row["ts_event"])
        # Advance the clock first, so ts_init reflects the tick's own instant.
        self._clock.advance_to(ts_event)
        seq = self._seq_by_symbol.get(symbol, 0)
        self._seq_by_symbol[symbol] = seq + 1
        return MarketTick(
            ts_event=ts_event,
            ts_init=self._clock.timestamp_ns(),
            symbol=symbol,
            # A recorded file is not a venue, but ``Decimal("NaN")`` is just as
            # valid a construction here, and replay feeds the tracer E2E and
            # every strategy test — a ``NaN`` price published from a file would
            # surface as an ``InvalidOperation`` from whichever guard compared
            # against it, blaming a layer that read the tick correctly. An
            # unreadable row raises, which is what replay has always done.
            price=exact_figure(Decimal(str(row["price"]))),
            size=exact_figure(Decimal(str(row["size"]))),
            aggressor_side=AggressorSide(row["aggressor_side"]),
            trade_id=str(row["trade_id"]),
            seq=seq,
        )
