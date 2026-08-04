"""The boot guards ``HyperliquidExchange.start()`` runs before anything else
reaches the venue (issue #179): the account abstraction-mode gate.

Driven through ``start()`` rather than against ``preflight`` directly — the gate
is a *lifecycle* refusal, and the seam that has to hold it is the ``Exchange``
one ADR-0024 step 4 calls. The POST transport stays the only fake, as everywhere
else in this suite, so the venue read under test is the one the adapter would
really send.

Why the gate exists at all (ADR-0046 §1): outside Manual/Standard the perps
clearinghouse reports only the collateral *posted into perps*, so account equity
and free margin read an order of magnitude low with nothing in the response
saying so. There is no number to sanity-check — only the mode.
"""

import asyncio
from decimal import Decimal

import pytest
from hyperliquid_fakes import FakeExchangeApi
from pydantic import SecretStr

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.domain import InstrumentSpec, VenueAccountModeUnsupported
from tickwright.venues.hyperliquid import (
    HyperliquidConfig,
    HyperliquidExchange,
    HyperliquidUniverse,
)

# Anvil's account #0 — a publicly-known throwaway key, safe in a test file.
TEST_SIGNING_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"

UNIVERSE = HyperliquidUniverse(
    specs={
        "BTC": InstrumentSpec(
            symbol="BTC", sz_decimals=5, max_decimals=6, min_notional=Decimal("10"), max_sig_figs=5
        )
    },
    asset_indices={"BTC": 3},
)

STARTUP_TIMEOUT_SECONDS = 60.0
"""The barrier budget the composition root hands the adapter (ADR-0044 §6): the
mode read is bounded by ``startup_reconciliation_timeout``, never a second one."""


def _exchange(post: FakeExchangeApi, *, clock: ManualClock | None = None) -> HyperliquidExchange:
    return HyperliquidExchange(
        config=HyperliquidConfig(
            testnet=True, symbols=["BTC"], signing_key=SecretStr(TEST_SIGNING_KEY)
        ),
        bus=InMemoryBus(),
        clock=clock if clock is not None else ManualClock(),
        universe=UNIVERSE,
        post=post,
        startup_timeout_seconds=STARTUP_TIMEOUT_SECONDS,
    )


@pytest.mark.parametrize("mode", ["unifiedAccount", "portfolioMargin"])
def test_a_pooled_account_mode_refuses_to_start_with_the_operator_s_remediation(
    mode: str,
) -> None:
    """ADR-0046 §3: the refusal is written as a **remediation**, not a complaint.

    ~88 % of sampled leaderboard accounts are on a mode this refuses, so the
    operator meeting this error is the common case rather than the exotic one —
    and the fix is two **user-signed** actions an agent wallet provably cannot
    perform (measured in #152: ``agent_set_abstraction`` comes back
    ``Abstraction transition not allowed``). The engine therefore cannot do it
    for itself, which is exactly why the error has to say what to do.

    Four things are asserted because all four are load-bearing to an operator
    reading one line of a crashed boot: the mode observed, **both** accepted
    literals, and both steps of the fix.
    """
    post = FakeExchangeApi({"userAbstraction": mode})

    with pytest.raises(VenueAccountModeUnsupported) as refusal:
        asyncio.run(_exchange(post).start())

    message = str(refusal.value)
    assert mode in message
    assert "default" in message and "disabled" in message
    assert "userSetAbstraction" in message
    assert "usdClassTransfer" in message
