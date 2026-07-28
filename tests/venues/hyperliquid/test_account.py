"""The Hyperliquid account declaration (ADR-0038/0042).

The one place a venue-native account identity becomes a ``domain``
``AccountSpec``: nothing else in the codebase composes a Hyperliquid account id.
"""

from decimal import Decimal

import pytest
from pydantic import SecretStr

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.domain import InstrumentSpec, Netting
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


def _exchange(*, testnet: bool, account_address: str | None = None) -> HyperliquidExchange:
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
    )


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
