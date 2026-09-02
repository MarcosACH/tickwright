"""Order pricing, the only caller of the rounding helper."""

from domain.rounding import round_to_tick
from domain.specs import SPECS


def limit_price(symbol: str, raw_price: float) -> float:
    """The price sent to the venue. Off-grid prices are rejected on arrival."""
    return round_to_tick(raw_price, SPECS[symbol].tick)
