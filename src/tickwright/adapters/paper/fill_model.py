"""The ``FillModel`` seam — a paper-internal boundary (ADR-0012).

Only ``PaperExchange`` calls it, so it lives here, not in ``domain`` (module-map
decision). It isolates the one nondeterminism-shaped decision so determinism is a
wiring choice: ``ImmediateFillModel`` (this slice) uses no RNG at all; the seeded
``StochasticFillModel`` lands in a later slice behind the same interface.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

from tickwright.domain import MarketTick, PlaceOrder


@dataclass(frozen=True, slots=True)
class Fill:
    """A fill decision: how much filled and at what price. Trade id is the
    exchange's to assign."""

    quantity: Decimal
    price: Decimal


@runtime_checkable
class FillModel(Protocol):
    """Decides whether and at what price/quantity an order fills."""

    def market_fill(self, order: PlaceOrder, tick: MarketTick) -> Fill:
        """Return the fill for a MARKET ``order`` against the latest ``tick``."""
        ...


class ImmediateFillModel:
    """Deterministic, optimistic, zero-slippage, full-fill (ADR-0012 default)."""

    def market_fill(self, order: PlaceOrder, tick: MarketTick) -> Fill:
        # Unlimited liquidity: fill the whole quantity now at the tick price.
        return Fill(quantity=order.quantity, price=tick.price)
