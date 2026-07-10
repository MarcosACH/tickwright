"""The opt-in live testnet suite (issue #23, ADR-0022): real venue, real key.

Runs only with ``TICKWRIGHT_HYPERLIQUID__SIGNING_KEY`` in the environment
(a funded testnet API wallet) and never in the CI gate — ``uv run pytest -m
live`` is the manual/nightly invocation. One round trip proves the whole
write path against the real venue: place a deep out-of-the-money resting
LIMIT → reconcile-visible via ``fetch_order`` → cancel by cloid →
``CANCELLED``, both as a pushed report and as venue truth.
"""

import asyncio
import os
from decimal import Decimal

import pytest
from pydantic import SecretStr

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import LiveClock
from tickwright.domain import (
    ExecutionReport,
    OrderState,
    OrderStatusReport,
    OrderType,
    PlaceOrder,
    Side,
    TimeInForce,
    derive_cloid,
    quantize_price,
    quantize_size,
)
from tickwright.venues.hyperliquid import (
    HyperliquidConfig,
    HyperliquidExchange,
    fetch_instrument_specs,
)
from tickwright.venues.hyperliquid.transport import post_json

SIGNING_KEY_ENV = "TICKWRIGHT_HYPERLIQUID__SIGNING_KEY"
ACCOUNT_ADDRESS_ENV = "TICKWRIGHT_HYPERLIQUID__ACCOUNT_ADDRESS"
SYMBOL = "BTC"

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        SIGNING_KEY_ENV not in os.environ,
        reason=f"no {SIGNING_KEY_ENV}: live testnet suite skipped (ADR-0022)",
    ),
]


def test_place_reconcile_cancel_round_trip_on_testnet() -> None:
    async def main() -> None:
        config = HyperliquidConfig(
            testnet=True,
            symbols=[SYMBOL],
            signing_key=SecretStr(os.environ[SIGNING_KEY_ENV]),
            account_address=os.environ.get(ACCOUNT_ADDRESS_ENV),
        )
        universe = await fetch_instrument_specs(config)
        spec = universe.specs[SYMBOL]

        bus = InMemoryBus()
        clock = LiveClock()
        exchange = HyperliquidExchange(config=config, bus=bus, clock=clock, universe=universe)
        reports: list[ExecutionReport] = []

        async def collect(report: ExecutionReport) -> None:
            reports.append(report)

        bus.subscribe(ExecutionReport, collect)

        # A deep out-of-the-money buy — half the mid — rests and cannot fill;
        # the size clears the venue's $10 minimum with margin at that price.
        mids = await post_json(f"{config.api_url}/info", {"type": "allMids"})
        assert isinstance(mids, dict)
        mid = Decimal(str(mids[SYMBOL]))
        price = quantize_price(mid / 2, Side.BUY, spec)
        quantity = quantize_size(Decimal("12") / price, spec)

        # A fresh cloid per run: the venue remembers old ones, and this saga
        # identity is exactly what reconcile-by-cloid keys on (ADR-0006).
        cloid = derive_cloid(f"live-test:{SYMBOL}:{clock.timestamp_ns()}")

        await exchange.place(
            PlaceOrder(
                cloid=cloid,
                symbol=SYMBOL,
                side=Side.BUY,
                quantity=quantity,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.GTC,
                price=price,
            )
        )
        (live,) = [r for r in reports if isinstance(r, OrderStatusReport)]
        assert live.status is OrderState.LIVE, f"expected a resting order, got {live!r}"
        assert live.cloid == cloid

        # Reconcile-visible: the fetch path sees the resting order as venue truth.
        view = await exchange.fetch_order(cloid)
        assert view is not None and view.status is not None
        assert view.status.status is OrderState.LIVE
        assert view.fills == ()

        await exchange.cancel(cloid)
        cancelled = [
            r
            for r in reports
            if isinstance(r, OrderStatusReport) and r.status is OrderState.CANCELLED
        ]
        assert cancelled, "the venue-accepted cancel must report CANCELLED"

        # And the venue agrees: the order record itself reads canceled.
        after = await exchange.fetch_order(cloid)
        assert after is not None and after.status is not None
        assert after.status.status is OrderState.CANCELLED

    asyncio.run(main())
