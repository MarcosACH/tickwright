"""The ``FillModel`` seam — a paper-internal boundary (ADR-0012).

Only ``PaperExchange`` calls it, so it lives here, not in ``domain`` (module-map
decision). It isolates the one nondeterminism-shaped decision so determinism is a
wiring choice: ``ImmediateFillModel`` uses no RNG at all; the seeded
``StochasticFillModel`` models slippage, queue position, partial fills, and
latency behind the same interface, deterministic per seed.
"""

import random
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

from tickwright.domain import Clock, InvariantViolation, MarketTick, PlaceOrder, Side


@dataclass(frozen=True, slots=True)
class Fill:
    """A fill decision: how much filled and at what price. Trade id is the
    exchange's to assign."""

    quantity: Decimal
    price: Decimal


@runtime_checkable
class FillModel(Protocol):
    """Decides whether and at what price/quantity an order fills.

    The methods are ``async`` so a model can model latency by awaiting the
    injected ``Clock`` (ADR-0005); ``limit_fill`` may return ``None`` to mean
    *not this tick* — a queue-position miss that leaves the order resting.
    """

    async def market_fill(self, order: PlaceOrder, tick: MarketTick) -> Fill:
        """Return the fill for a MARKET ``order`` against the latest ``tick``."""
        ...

    async def limit_fill(self, order: PlaceOrder, tick: MarketTick) -> Fill | None:
        """Return the fill for a crossing LIMIT ``order`` (the book decides it
        crosses; the model decides how much, at what price, and *whether* it
        fills this tick — ``None`` means it stays resting for a later one)."""
        ...


class ImmediateFillModel:
    """Deterministic, optimistic, zero-slippage, full-fill (ADR-0012 default)."""

    async def market_fill(self, order: PlaceOrder, tick: MarketTick) -> Fill:
        # Unlimited liquidity: fill the whole quantity now at the tick price.
        return Fill(quantity=order.quantity, price=tick.price)

    async def limit_fill(self, order: PlaceOrder, tick: MarketTick) -> Fill:
        # Full fill at the limit price: no price improvement, no partials. The
        # book has already decided the order crosses, so ``price`` is set.
        if order.price is None:
            raise InvariantViolation(f"marketable LIMIT {order.cloid} filled with no price")
        return Fill(quantity=order.quantity, price=order.price)


class StochasticFillModel:
    """Seeded realism behind the same seam (ADR-0012): slippage, queue position,
    partial fills, and latency — all deterministic because the RNG and ``Clock``
    are injected.

    Determinism is the whole point: draws come *only* from ``rng``, so a fixed
    seed replays a byte-identical fill sequence and a different seed diverges.
    """

    def __init__(
        self,
        *,
        rng: random.Random,
        clock: Clock,
        prob_slippage: float = 0.0,
        max_slippage: Decimal = Decimal("0"),
        prob_fill_on_limit: float = 1.0,
        partial_fill_fraction: Decimal = Decimal("1"),
        latency_seconds: float = 0.0,
    ) -> None:
        self._rng = rng
        self._clock = clock
        self._prob_slippage = prob_slippage
        self._max_slippage = max_slippage
        self._prob_fill_on_limit = prob_fill_on_limit
        self._partial_fill_fraction = partial_fill_fraction
        self._latency_seconds = latency_seconds

    async def market_fill(self, order: PlaceOrder, tick: MarketTick) -> Fill:
        # Market takes liquidity now — always the full quantity (ADR-0012 scopes
        # partials to the resting book), but the price may slip adversely.
        await self._clock.sleep(self._latency_seconds)
        return Fill(quantity=order.quantity, price=self._slipped(order.side, tick.price))

    async def limit_fill(self, order: PlaceOrder, tick: MarketTick) -> Fill | None:
        # A resting LIMIT is a maker: it fills at its own price, never slipped.
        # Two seeded decisions: whether it fills *this* crossing tick at all
        # (queue position — someone may be ahead of us) and, if so, what
        # fraction of the original size. The exchange caps the fraction to the
        # remaining and re-rests any remainder, so partials converge to FILLED.
        if order.price is None:
            raise InvariantViolation(f"crossing LIMIT {order.cloid} priced with no price")
        if self._rng.random() >= self._prob_fill_on_limit:
            return None  # queue miss: stays resting for a later crossing tick
        await self._clock.sleep(self._latency_seconds)
        return Fill(quantity=order.quantity * self._partial_fill_fraction, price=order.price)

    def _slipped(self, side: Side, price: Decimal) -> Decimal:
        """Perturb ``price`` against ``side`` with probability ``prob_slippage``.

        Slippage is adverse by construction — a BUY pays up, a SELL receives
        less — with a magnitude drawn uniformly in ``[0, max_slippage]`` so the
        seed alone determines the exact fill price.
        """
        if self._rng.random() >= self._prob_slippage:
            return price
        magnitude = Decimal(str(self._rng.random())) * self._max_slippage
        adverse = Decimal(1) + magnitude if side is Side.BUY else Decimal(1) - magnitude
        return price * adverse
