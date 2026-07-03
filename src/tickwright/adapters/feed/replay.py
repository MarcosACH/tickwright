"""``ReplayFeed`` — the hermetic, non-venue ``MarketFeed`` (ADR-0027).

Reads newline-delimited JSON (one ``MarketTick`` per line — readable, diffable)
and, for each row, advances the injected ``ReplayClock`` to the row's ``ts_event``
before publishing. Replay is therefore deterministic in time and never conflates
(ADR-0023): every recorded tick is delivered in file order. The tracer E2E and
every strategy test stand on this feed.
"""

import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import TypedDict

from tickwright.domain import AggressorSide, EventBus, MarketTick, ReplayClock


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
            tick = self._to_tick(row)
            await self._bus.publish(tick)

    async def stop(self) -> None:  # noqa: B027 - replay has no live resources to release.
        """No-op: a replay drains at ``start`` and holds nothing open."""

    def _read_rows(self) -> Iterator[_TickRow]:
        with self._path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    yield json.loads(line)

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
            price=Decimal(str(row["price"])),
            size=Decimal(str(row["size"])),
            aggressor_side=AggressorSide(row["aggressor_side"]),
            trade_id=str(row["trade_id"]),
            seq=seq,
        )
