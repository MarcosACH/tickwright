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
    post = FakeExchangeApi({"meta": meta_response()})
    universe = asyncio.run(fetch_instrument_specs(HyperliquidConfig(testnet=True), post=post))

    assert post.requests == [("https://api.hyperliquid-testnet.xyz/info", {"type": "meta"})]
    assert universe.specs["BTC"] == InstrumentSpec(
        symbol="BTC",
        sz_decimals=5,
        max_decimals=6,
        min_notional=Decimal("10"),
        max_sig_figs=5,
        # Both sourced from the venue as of ADR-0040 §4; asserted for their own
        # sake in the margin-fields test below.
        max_leverage=40,
        margin_maint=Decimal("0.0125"),
    )
    assert universe.specs["ETH"].sz_decimals == 4
    # Perps are addressed by their position in the universe array (ADR-0030).
    assert universe.asset_indices == {"BTC": 0, "ETH": 1}


def test_the_venue_publishes_the_leverage_cap_and_the_maintenance_rate_it_implies() -> None:
    """``maxLeverage`` is a real venue fact — the bound ADR-0044 §9 validates the
    operator's configured leverage against — and ``margin_maint`` is the flat
    tier-0 maintenance rate it implies, ``1/(2·max_leverage)`` (ADR-0040 §4).

    The rate is carried as **explicit data** rather than derived downstream, so
    the ``domain`` maintenance helper stays venue-agnostic instead of learning
    Hyperliquid's "half the initial margin at max leverage" rule — the choice
    ADR-0036 made for the fee rates.

    Sourcing it here is what makes the live half of the bounds check work at
    all: left at ``InstrumentSpec``'s ``1`` default, every live spec would
    refuse any configured leverage above ``1x``.

    The last assertion is the venue's own arithmetic, not ours: ADR-0040 §4
    reproduces ``margin_maint`` against a real testnet BTC position, where a
    notional of ``5873.49`` drew a reported maintenance of ``73.418625``.
    """
    post = FakeExchangeApi({"meta": meta_response()})

    universe = asyncio.run(fetch_instrument_specs(HyperliquidConfig(testnet=True), post=post))

    assert universe.specs["BTC"].max_leverage == 40
    assert universe.specs["BTC"].margin_maint == Decimal("0.0125")
    assert universe.specs["ETH"].max_leverage == 25
    assert universe.specs["ETH"].margin_maint == Decimal("0.02")
    assert Decimal("5873.49") * universe.specs["BTC"].margin_maint == Decimal("73.418625")
