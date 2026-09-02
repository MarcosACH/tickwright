"""Venue-sourced instrument specs.

Tick sizes are transcribed from the venue meta endpoint. Only BTC's is not a
power of ten, which is the whole point of this tree: a change that is correct
for 0.01 and 0.001 can still be wrong for 0.5, and no symbol search says so.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class InstrumentSpec:
    """One symbol's trading rules."""

    symbol: str
    tick: float


SPECS = {
    "BTC": InstrumentSpec("BTC", 0.5),
    "ETH": InstrumentSpec("ETH", 0.01),
    "SOL": InstrumentSpec("SOL", 0.001),
}
