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
from tickwright.observability import NamedEvent
from tickwright.observability.testing import capture_events
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

# The recorded isolated snapshot: the same 0.002 BTC long at 5x, entry 64815,
# mark 64794, with 25.898067 of collateral locked into its own bucket. Measured
# (#142 §4): ``marginUsed = positionValue + rawUsd = 129.588 − 103.731933 =
# 25.856067`` and ``rawUsd = collateral − entryPx·|szi| = 25.898067 − 129.63``,
# so ``unrealizedPnl`` is ``129.588 − 129.63 = −0.042``. The whole balance sits
# in the bucket, so the cross summary is empty and the cross maintenance figure
# reads 0.0 against a position that plainly has maintenance (ADR-0046 §2.1).
# ``liquidationPx`` is the venue's own, reproduced by the canonical formula to
# 28 significant figures and invariant to the mark.
ISOLATED_SNAPSHOT: dict = {
    "assetPositions": [
        {
            "type": "oneWay",
            "position": {
                "coin": "BTC",
                "szi": "0.002",
                "entryPx": "64815.0",
                "positionValue": "129.588",
                "unrealizedPnl": "-0.042",
                "returnOnEquity": "-0.0016217",
                "marginUsed": "25.856067",
                "liquidationPx": "52522.497721519",
                "maxLeverage": 40,
                "leverage": {"type": "isolated", "value": 5, "rawUsd": "-103.731933"},
                "cumFunding": {"allTime": "0.0", "sinceOpen": "0.0", "sinceChange": "0.0"},
            },
        }
    ],
    "crossMaintenanceMarginUsed": "0.0",
    "crossMarginSummary": {
        "accountValue": "0.0",
        "totalMarginUsed": "0.0",
        "totalNtlPos": "0.0",
        "totalRawUsd": "0.0",
    },
    "marginSummary": {
        "accountValue": "25.856067",
        "totalMarginUsed": "25.856067",
        "totalNtlPos": "129.588",
        "totalRawUsd": "-103.731933",
    },
    "time": 1_730_000_060_000,
    "withdrawable": "0.0",
}


def _without(snapshot: dict, field: str) -> dict:
    """``snapshot`` with one root field deleted — a venue response we cannot read."""
    return {name: value for name, value in snapshot.items() if name != field}


def _position_without(snapshot: dict, field: str) -> dict:
    """``snapshot`` with one field deleted from its position row: the same
    unreadable-response case one level down, where a partial parse would
    otherwise report a position the venue never described."""
    (entry,) = snapshot["assetPositions"]
    return snapshot | {"assetPositions": [entry | {"position": _without(entry["position"], field)}]}


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


def test_free_margin_ignores_withdrawable_once_an_order_is_resting() -> None:
    """The one snapshot where the two answers part company — and the state of a
    running engine, not a corner of one.

    Resting-order margin is deducted from ``withdrawable`` and from nothing else
    in the response, so the recorded account plus **one** exposure-increasing
    order moves that field without moving either summary. The order is
    ADR-0046 §2's measured one — 128.40 notional at 5x, `25.68` of initial
    margin — and the venue's withdrawal rule, ``max(0, accountValue − max(IM,
    0.1 × totalNtlPos))``, then floors the field at zero: ``25.9264 −
    max(25.9168 + 25.68, 12.9584)`` is negative. Reading it would report a
    healthy account as having no free collateral at all, and ADR-0024 leaves
    resting orders on the venue across a graceful stop — so nothing about this
    is exceptional.
    """
    state = _fetch_state(CROSS_SNAPSHOT | {"withdrawable": "0.0"})

    assert state is not None
    assert state.free_margin == Decimal("0.0096")


def test_a_cross_position_normalizes_to_the_measured_row_and_holds_no_collateral() -> None:
    """The position half: the signed size the venue reports, the mark notional,
    and the unrealized PnL — with ``isolated_collateral`` ``None``, because a
    cross position is backed by the account pool and has no bucket of its own
    (ADR-0040 §7: isolated buckets are the locked, excluded ones).

    Recorded: 0.002 long at entry 64809, mark 64792, so the venue's
    ``positionValue`` is 129.584 and its ``unrealizedPnl`` is
    ``129.584 − 0.002 × 64809 = −0.034`` — the arithmetic is the venue's, not
    this normalizer's.
    """
    state = _fetch_state(CROSS_SNAPSHOT)

    assert state is not None
    (position,) = state.positions
    assert position.symbol == "BTC"
    assert position.signed_size == Decimal("0.002")
    assert position.entry_price == Decimal("64809.0")
    assert position.notional == Decimal("129.584")
    assert position.unrealized_pnl == Decimal("-0.034")
    assert position.margin_used == Decimal("25.9168")
    assert position.isolated_collateral is None


def test_an_isolated_long_recovers_positive_collateral_from_a_negative_raw_usd_leg() -> None:
    """The trap the second measurement round closed.

    ``leverage.rawUsd`` looks like the position's locked collateral and is not:
    it is the cash leg **net of cost basis**, so for a long it measures
    *negative* — `−103.731933` against a collateral of `25.898067` on this very
    position. Reading it would report a funded position as owing the venue a
    hundred dollars. The collateral is recovered as ``marginUsed −
    unrealizedPnl``, which is the venue's own arithmetic read the other way
    round (``marginUsed`` ≡ ``isolated_collateral + unrealizedPnl``, and it moves
    with the mark while ``rawUsd`` holds).
    """
    state = _fetch_state(ISOLATED_SNAPSHOT)

    assert state is not None
    (position,) = state.positions
    assert position.isolated_collateral == Decimal("25.898067")


def test_the_venue_liquidation_price_is_passed_through_verbatim() -> None:
    """Read, never recomputed: re-deriving it needs the maintenance-margin tier
    fixed point, and the venue already publishes the number exactly (ADR-0040
    §3). So the digits that arrive are the digits that leave."""
    state = _fetch_state(ISOLATED_SNAPSHOT)

    assert state is not None
    (position,) = state.positions
    assert position.liquidation_price == Decimal("52522.497721519")


def test_an_absent_liquidation_price_stays_absent() -> None:
    """The recorded cross long came back with no liquidation price at all, and
    that is the **majority** reading for a long — 12 of 17 cross longs across 22
    mainnet accounts, every null a long and not one a short (ADR-0046 §6).

    So the absence is ordinary, and substituting anything for it — a zero, the
    mark, a computed price — would hand a strategy a fabricated liquidation level
    in the common case.
    """
    state = _fetch_state(CROSS_SNAPSHOT)

    assert state is not None
    (position,) = state.positions
    assert position.liquidation_price is None


def test_a_transport_failure_reads_as_no_venue_truth_never_as_a_flat_book() -> None:
    """The connectivity guard in the return type (ADR-0011 inv 1).

    An unreachable venue is *no truth to compare against*, and the reconcile
    freezes on it. A zero-filled state would be fail-open — the fabricated flat
    ADR-0034 forbids — and would heal a restored ledger down to nothing.
    """
    assert _fetch_state(TimeoutError("the venue never answered")) is None


@pytest.mark.parametrize(
    ("label", "response"),
    [
        ("not a response at all", {"status": "err"}),
        (
            "no cross summary to take the difference of",
            _without(CROSS_SNAPSHOT, "crossMarginSummary"),
        ),
        ("no positions list", _without(CROSS_SNAPSHOT, "assetPositions")),
        ("a position row missing a field", _position_without(CROSS_SNAPSHOT, "marginUsed")),
    ],
)
def test_an_unparseable_response_reads_as_no_venue_truth_and_is_named(
    label: str, response: object
) -> None:
    """A shape we cannot read is a *failed* read, not a flat account.

    This is where a venue contract change lands, so it must stay visible rather
    than degrade quietly into a heal: ``None`` freezes the cycle, and the named
    event is what tells an operator the response — not the connection — is what
    changed.
    """
    with capture_events() as events:
        state = _fetch_state(response)

    assert state is None, label
    failed = [e for e in events if e["event"] == NamedEvent.EXCHANGE_REQUEST_FAILED]
    assert failed and failed[0]["request"] == "clearinghouseState", label
