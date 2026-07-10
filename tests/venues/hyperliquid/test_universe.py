"""Instrument-spec sourcing from the venue meta endpoint (issue #23, ADR-0030/0031).

The meta ``universe`` array is the venue's own instrument catalog: each entry
becomes one ``InstrumentSpec`` (``szDecimals`` from the venue; perps'
``MAX_DECIMALS = 6`` and 5-sig-fig price rule; the $10 minimum order value),
and each position becomes the asset index every order action addresses.
"""

import asyncio
from decimal import Decimal

from hyperliquid_fakes import FakeExchangeApi

from tickwright.domain import InstrumentSpec
from tickwright.venues.hyperliquid import HyperliquidConfig, fetch_instrument_specs


def meta_response() -> dict:
    return {
        "universe": [
            {"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
            {"name": "ETH", "szDecimals": 4, "maxLeverage": 25},
        ]
    }


def test_fetch_instrument_specs_maps_the_meta_universe() -> None:
    post = FakeExchangeApi([meta_response()])
    universe = asyncio.run(fetch_instrument_specs(HyperliquidConfig(testnet=True), post=post))

    assert post.requests == [("https://api.hyperliquid-testnet.xyz/info", {"type": "meta"})]
    assert universe.specs["BTC"] == InstrumentSpec(
        symbol="BTC",
        sz_decimals=5,
        max_decimals=6,
        min_notional=Decimal("10"),
        max_sig_figs=5,
    )
    assert universe.specs["ETH"].sz_decimals == 4
    # Perps are addressed by their position in the universe array (ADR-0030).
    assert universe.asset_indices == {"BTC": 0, "ETH": 1}
