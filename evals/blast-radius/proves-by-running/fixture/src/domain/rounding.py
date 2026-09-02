"""Price rounding onto the venue's tick grid."""

from decimal import ROUND_HALF_EVEN, Decimal


def round_to_tick(price: float, tick: float) -> float:
    """Round `price` onto the `tick` grid."""
    return float(Decimal(str(price)).quantize(Decimal(str(tick)), rounding=ROUND_HALF_EVEN))
