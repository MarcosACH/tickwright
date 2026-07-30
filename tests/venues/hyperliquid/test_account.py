"""The Hyperliquid account boundary (ADR-0038/0042; ADR-0040/0046 for the state).

The one place a venue-native account identity becomes a ``domain``
``AccountSpec``, and the one place ``clearinghouseState`` becomes a
``VenueAccountState``: nothing else in the codebase composes a Hyperliquid
account id or names a Hyperliquid field for these quantities.

The two ``clearinghouseState`` bodies below are **recorded**, from the funded
testnet position [#142](https://github.com/MarcosACH/tickwright/issues/142)
measured — one cross, one isolated. Every figure in them is a measured venue
number or is forced by one, so the expected values these tests assert are the
venue's own arithmetic and not this normalizer's restated.
"""

import asyncio
from decimal import Decimal

import pytest
from hyperliquid_fakes import FakeExchangeApi
from pydantic import SecretStr

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.domain import InstrumentSpec, Netting, VenueAccountState
from tickwright.venues.hyperliquid import (
    HyperliquidConfig,
    HyperliquidExchange,
    HyperliquidUniverse,
)

# Anvil's account #0 — a publicly-known throwaway key, safe in a test file. Its
# address is a fixed function of the key, so it is an independent expected value.
TEST_SIGNING_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
WALLET_ADDRESS = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"

UNIVERSE = HyperliquidUniverse(
    specs={
        "BTC": InstrumentSpec(
            symbol="BTC", sz_decimals=5, max_decimals=6, min_notional=Decimal("10"), max_sig_figs=5
        )
    },
    asset_indices={"BTC": 3},
)

# The recorded cross snapshot: 0.002 BTC long at 5x, entry 64809, mark 64792.
# Measured (#142 §2): accountValue = totalRawUsd + totalNtlPos = −103.6576 +
# 129.584 = 25.9264, withdrawable = accountValue − totalMarginUsed = 0.0096, and
# crossMaintenanceMarginUsed = 129.584 × 1/(2·40) = 1.6198. Cross-only, so
# ``marginSummary`` and ``crossMarginSummary`` are the same numbers (research §2).
# ``liquidationPx`` came back ``null`` — the majority case for a long
# (ADR-0046 §6), and the reason this snapshot doubles as the pass-through case.
CROSS_SNAPSHOT: dict = {
    "assetPositions": [
        {
            "type": "oneWay",
            "position": {
                "coin": "BTC",
                "szi": "0.002",
                "entryPx": "64809.0",
                "positionValue": "129.584",
                "unrealizedPnl": "-0.034",
                "returnOnEquity": "-0.0026231",
                "marginUsed": "25.9168",
                "liquidationPx": None,
                "maxLeverage": 40,
                "leverage": {"type": "cross", "value": 5},
                "cumFunding": {"allTime": "0.0", "sinceOpen": "0.0", "sinceChange": "0.0"},
            },
        }
    ],
    "crossMaintenanceMarginUsed": "1.6198",
    "crossMarginSummary": {
        "accountValue": "25.9264",
        "totalMarginUsed": "25.9168",
        "totalNtlPos": "129.584",
        "totalRawUsd": "-103.6576",
    },
    "marginSummary": {
        "accountValue": "25.9264",
        "totalMarginUsed": "25.9168",
        "totalNtlPos": "129.584",
        "totalRawUsd": "-103.6576",
    },
    "time": 1_730_000_000_000,
    "withdrawable": "0.0096",
}


def _exchange(
    *,
    testnet: bool,
    account_address: str | None = None,
    post: FakeExchangeApi | None = None,
) -> HyperliquidExchange:
    return HyperliquidExchange(
        config=HyperliquidConfig(
            testnet=testnet,
            symbols=["BTC"],
            signing_key=SecretStr(TEST_SIGNING_KEY),
            account_address=account_address,
        ),
        bus=InMemoryBus(),
        clock=ManualClock(),
        universe=UNIVERSE,
        **({"post": post} if post is not None else {}),
    )


def _fetch_state(response: object) -> VenueAccountState | None:
    """The account read as the reconciler makes it: through the real adapter,
    with the POST transport — the process boundary — the only fake."""

    async def main() -> VenueAccountState | None:
        exchange = _exchange(testnet=True, post=FakeExchangeApi({"clearinghouseState": response}))
        return await exchange.fetch_account_state()

    return asyncio.run(main())


@pytest.mark.parametrize(
    ("testnet", "network"),
    [(True, "testnet"), (False, "mainnet")],
)
def test_the_account_id_is_qualified_by_venue_network_and_address(
    testnet: bool, network: str
) -> None:
    """Three segments where paper's is two, so the two are never confusable."""
    spec = _exchange(testnet=testnet).account_spec()

    assert spec.account_id == f"hyperliquid-{network}-{WALLET_ADDRESS}"
    assert spec.netting is Netting.NET


def test_the_id_names_the_trading_account_not_the_agent_wallet() -> None:
    """An API/agent wallet signs *for* a master account; the ledger belongs to
    the account traded, which is what every /info query asks about too."""
    spec = _exchange(testnet=True, account_address="0xMASTER").account_spec()

    assert spec.account_id == "hyperliquid-testnet-0xMASTER"


def test_the_live_path_declares_no_genesis_collateral() -> None:
    """Live's opening state is ingested from the venue, never configured — and
    the ``None`` is the predicate the startup checks read (ADR-0042 §6)."""
    assert _exchange(testnet=True).account_spec().genesis_collateral is None


def test_a_recorded_cross_snapshot_normalizes_to_the_measured_account_figures() -> None:
    """Equity, free margin and the cross maintenance figure, each from the field
    ADR-0046 §2/§2.1 pins — the three account-grain numbers the reconcile
    compares against.

    ``equity`` is ``marginSummary.accountValue`` (whole account, isolated
    included); ``free_margin`` is the ``crossMarginSummary`` difference;
    ``cross_maintenance_margin`` is the root cross-only figure, named for the
    subset it covers so no caller can mistake it for a Σ over all positions.
    """
    state = _fetch_state(CROSS_SNAPSHOT)

    assert state is not None
    assert state.equity == Decimal("25.9264")
    assert state.free_margin == Decimal("0.0096")
    assert state.cross_maintenance_margin == Decimal("1.6198")
