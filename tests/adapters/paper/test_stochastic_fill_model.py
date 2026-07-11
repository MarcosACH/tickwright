"""``StochasticFillModel`` — the seeded second ``FillModel`` (ADR-0012).

A seeded RNG and the injected ``Clock`` make every fill decision deterministic:
same seed + same tick stream ⇒ byte-identical fills; different seed ⇒ a different
sequence. Slippage, queue position, partial fills, and latency are the four
nondeterminism-shaped decisions it owns behind the same interface the default
``ImmediateFillModel`` satisfies — so realism stays a wiring choice.
"""

import asyncio
import random
from decimal import Decimal

from tickwright.adapters.clock import ManualClock
from tickwright.adapters.paper import StochasticFillModel
from tickwright.domain import (
    MarketTick,
    OrderType,
    PlaceOrder,
    Side,
    TimeInForce,
)


def _tick(price: str, ts: int = 1_000, symbol: str = "BTC") -> MarketTick:
    from tickwright.domain import AggressorSide

    return MarketTick(
        ts_event=ts,
        ts_init=ts,
        symbol=symbol,
        price=Decimal(price),
        size=Decimal("5"),
        aggressor_side=AggressorSide.BUY,
        trade_id=f"t{ts}",
        seq=0,
    )


def _market_order(qty: str = "1", symbol: str = "BTC", cloid: str = "0xabc") -> PlaceOrder:
    return PlaceOrder(
        cloid=cloid,
        symbol=symbol,
        side=Side.BUY,
        quantity=Decimal(qty),
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.IOC,
    )


def _model(seed: int) -> StochasticFillModel:
    # prob_slippage=1.0 so the seed's draw is always observable in the price.
    return StochasticFillModel(
        rng=random.Random(seed),
        clock=ManualClock(),
        prob_slippage=1.0,
        max_slippage=Decimal("0.001"),
    )


def test_same_seed_gives_byte_identical_market_fills() -> None:
    order = _market_order()
    tick = _tick("42000")

    first = asyncio.run(_model(7).market_fill(order, tick))
    again = asyncio.run(_model(7).market_fill(order, tick))

    assert first == again  # same seed + same tick ⇒ byte-identical fill


def test_a_different_seed_gives_a_different_market_fill() -> None:
    order = _market_order()
    tick = _tick("42000")

    seven = asyncio.run(_model(7).market_fill(order, tick))
    ninety_nine = asyncio.run(_model(99).market_fill(order, tick))

    assert seven != ninety_nine  # the seed is what makes the sequence differ
