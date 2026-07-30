"""The Hyperliquid account boundary (ADR-0038/0042; ADR-0040/0046 for the state).

The one place a venue-native account identity becomes a ``domain``
``AccountSpec``, and the one place ``clearinghouseState`` becomes a
``VenueAccountState``: nothing else in the codebase composes a Hyperliquid
account id or names a Hyperliquid field for these quantities.

Three of the four ``clearinghouseState`` bodies below are **recorded**: two from
the funded testnet position [#142](https://github.com/MarcosACH/tickwright/issues/142)
measured — one cross, one isolated — and one from a sampled mainnet account
holding both modes at once, which is the only shape that can tell the two
account-grain summaries apart. The fourth is derived and says so. Every figure is
a measured venue number or is forced by one, so the expected values these tests
assert are the venue's own arithmetic and not this normalizer's restated.
"""

import ast
import asyncio
from decimal import Decimal
from pathlib import Path

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

# The same account with its one position closed — the ordinary shape of a funded
# account that holds nothing, and the *first* response a live startup barrier
# ever reads. The venue drops a flat coin from ``assetPositions`` entirely rather
# than reporting a zero row, so the list is empty; the whole balance is cash, and
# the venue's own identity still holds: accountValue = totalRawUsd + totalNtlPos
# = 25.9264 + 0. Not a measured snapshot but forced by one — every figure is the
# cross snapshot's with the position's contribution removed.
FLAT_SNAPSHOT: dict = {
    "assetPositions": [],
    "crossMaintenanceMarginUsed": "0.0",
    "crossMarginSummary": {
        "accountValue": "25.9264",
        "totalMarginUsed": "0.0",
        "totalNtlPos": "0.0",
        "totalRawUsd": "25.9264",
    },
    "marginSummary": {
        "accountValue": "25.9264",
        "totalMarginUsed": "0.0",
        "totalNtlPos": "0.0",
        "totalRawUsd": "25.9264",
    },
    "time": 1_730_000_120_000,
    "withdrawable": "25.9264",
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


# A **mainnet** account holding one cross short and one isolated long at once —
# recorded from `0x92ae…86a8`, sampled from the public leaderboard, because it is
# the shape the two testnet bodies above cannot express between them and the one
# where every account-grain mapping becomes falsifiable:
#
# - ``marginSummary`` and ``crossMarginSummary`` finally **disagree**
#   (`50937.813103` vs `20348.018132`), where both single-mode bodies above carry
#   the same numbers in each — so this is the only fixture that can tell
#   ADR-0046 §2's equity source from the wrong one.
# - ADR-0046 §2's cancellation is reproduced independently: ``equity − Σ
#   marginUsed`` = `50937.813103 − 52258.194971` = **`−1320.381868`**, exactly
#   the venue's own ``crossAV − crossTMU``. Negative, and ``withdrawable`` is
#   floored at `0.0` — §2's two reasons for not reading that field, in one body.
# - §2.1's asymmetry, measured inside one response:
#   ``marginSummary.totalMarginUsed`` = `52258.194971` = Σ over **both**
#   positions, while ``crossMaintenanceMarginUsed`` = `5417.1` =
#   `108342.0 × 1/(2·10)` — the cross position's alone, the isolated one's
#   `1/(2·3) × 46910.0` contributing **nothing**.
# - The ``rawUsd`` trap again, and on this account it is a *long* whose bucket is
#   funded: collateral `30589.794971 − (−6841.452781)` = `37431.247752` against a
#   ``rawUsd`` of `−16320.205029`.
# - Both directions in one body: ``szi`` `−2000.0` and `+1000000.0`, and the
#   short's ``liquidationPx`` is non-null as ADR-0046 §6 requires it to be.
#
# One caveat this body is the first to expose: the identity the two comments above
# state as ``accountValue = totalRawUsd + totalNtlPos`` is **long-only**.
# ``totalNtlPos`` is an unsigned Σ of ``positionValue``, so the identity needs the
# *signed* sum — here `112369.813103 + (−108342.0 + 46910.0)` = `50937.813103`.
# Measured across 431 leaderboard accounts holding positions: the unsigned form
# holds on 203/203 long-only accounts and **0/228** holding any short, while the
# signed form holds on all 228 to ~1e-9. Neither field is read by this normalizer;
# they are here for provenance, and the note is so the file does not teach the
# unsigned form as a general rule.
MIXED_SNAPSHOT: dict = {
    "assetPositions": [
        {
            "type": "oneWay",
            "position": {
                "coin": "HYPE",
                "szi": "-2000.0",
                "entryPx": "57.1359",
                "positionValue": "108342.0",
                "unrealizedPnl": "5929.91487",
                "returnOnEquity": "0.2594651046",
                "marginUsed": "21668.4",
                "liquidationPx": "426.1797710395",
                "maxLeverage": 10,
                "leverage": {"type": "cross", "value": 5},
                "cumFunding": {
                    "allTime": "10373.419637",
                    "sinceOpen": "-69.628649",
                    "sinceChange": "-63.075936",
                },
            },
        },
        {
            "type": "oneWay",
            "position": {
                "coin": "CASHCAT",
                "szi": "1000000.0",
                "entryPx": "0.053751",
                "positionValue": "46910.0",
                "unrealizedPnl": "-6841.452781",
                "returnOnEquity": "-0.2545588045",
                "marginUsed": "30589.794971",
                "liquidationPx": "0.019584246",
                "maxLeverage": 3,
                "leverage": {"type": "isolated", "value": 2, "rawUsd": "-16320.205029"},
                "cumFunding": {
                    "allTime": "254.350734",
                    "sinceOpen": "5.869039",
                    "sinceChange": "6.733688",
                },
            },
        },
    ],
    "crossMaintenanceMarginUsed": "5417.1",
    "crossMarginSummary": {
        "accountValue": "20348.018132",
        "totalMarginUsed": "21668.4",
        "totalNtlPos": "108342.0",
        "totalRawUsd": "128690.018132",
    },
    "marginSummary": {
        "accountValue": "50937.813103",
        "totalMarginUsed": "52258.194971",
        "totalNtlPos": "155252.0",
        "totalRawUsd": "112369.813103",
    },
    "time": 1_785_377_282_053,
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
        # No routes by default: the ``account_spec`` tests below reach the venue
        # for nothing, and an unrouted request is a loud failure rather than a
        # silent one against a real socket.
        post=post if post is not None else FakeExchangeApi({}),
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


def test_the_isolated_snapshot_takes_equity_from_the_whole_account_not_the_cross_pool() -> None:
    """The first body where the two summaries disagree, so the first that can tell
    ADR-0046 §2's equity source from the wrong one.

    The recorded cross body carries identical numbers in ``marginSummary`` and
    ``crossMarginSummary`` (it holds no isolated position, so there is nothing to
    differ), which means it cannot distinguish them. Here the whole balance sits in
    the position's own bucket: equity is `25.856067` and the **cross pool is
    empty**, so an equity read narrowed to ``crossMarginSummary`` would report this
    funded account as worth nothing and heal a restored ledger down to it.

    The cross maintenance figure reading `0.0` against a position that plainly has
    maintenance is ADR-0046 §2.1's trap, asserted rather than described: the venue
    publishes no isolated counterpart, so this is the honest reading and not a bug
    to be corrected downstream.
    """
    state = _fetch_state(ISOLATED_SNAPSHOT)

    assert state is not None
    assert state.equity == Decimal("25.856067")
    assert state.free_margin == Decimal("0.0")
    assert state.cross_maintenance_margin == Decimal("0.0")


def test_a_mixed_book_reads_equity_over_all_positions_and_free_margin_over_the_cross_pool() -> None:
    """Both account-grain mappings against a body where every wrong source gives a
    different, visibly wrong number (ADR-0046 §2/§2.1).

    Equity is the whole account including the isolated bucket (`50937.813103`),
    while free margin is the cross pair alone and comes out **negative**
    (`−1320.381868`) — which the venue's own arithmetic confirms twice over: it is
    exactly ``equity − Σ marginUsed``, §2's cancellation, and this account's
    ``withdrawable`` is floored at `0.0`, so reading that field instead would call
    a book with 50k of equity flat broke.

    The cross maintenance figure covers the cross position only: `5417.1` is
    `108342.0 × 1/(2·10)`, and the isolated position's maintenance is absent from
    it entirely (§2.1).
    """
    state = _fetch_state(MIXED_SNAPSHOT)

    assert state is not None
    assert state.equity == Decimal("50937.813103")
    assert state.free_margin == Decimal("-1320.381868")
    assert state.cross_maintenance_margin == Decimal("5417.1")


def test_a_mixed_book_keeps_both_rows_in_venue_order_with_each_mode_read_as_its_own() -> None:
    """Two positions in one response, one per mode — the discrimination every
    single-mode fixture can only make in isolation.

    ``isolated_collateral`` is the field that says which is which, so both arms
    have to be right *within one read*: ``None`` for the cross row because it is
    backed by the account pool, and the recovered bucket for the isolated one.
    That recovery is the ``rawUsd`` trap on this body too, and here on a **long**:
    `30589.794971 − (−6841.452781)` = `37431.247752`, where ``rawUsd`` reads
    `−16320.205029`.

    Row order is the venue's, since nothing here may sort or de-duplicate a book
    the venue nets per coin.
    """
    state = _fetch_state(MIXED_SNAPSHOT)

    assert state is not None
    cross, isolated = state.positions
    assert (cross.symbol, isolated.symbol) == ("HYPE", "CASHCAT")
    assert cross.isolated_collateral is None
    assert cross.margin_used == Decimal("21668.4")
    assert isolated.isolated_collateral == Decimal("37431.247752")
    assert isolated.margin_used == Decimal("30589.794971")


def test_a_short_keeps_the_direction_the_venue_reports_it_with() -> None:
    """``signed_size`` is the only thing a consumer can reconstruct side from.

    Our own ledger keeps a magnitude and rides the side on the saga, so the sign
    the venue reports is the whole of the direction crossing this boundary. Drop it
    and a reconcile heals an open short into a long of the same size — a sign flip
    is the worst outcome available on this surface, worse than freezing.

    The short also pins ADR-0046 §6's other half, which no long can state: a
    short's liquidation price sits *above* the mark and is therefore **never**
    null, where 12 of 17 cross longs read null. So an absent liquidation price is
    the ordinary reading for a long and an impossible one here.
    """
    state = _fetch_state(MIXED_SNAPSHOT)

    assert state is not None
    short, long = state.positions
    assert short.signed_size == Decimal("-2000.0")
    assert long.signed_size == Decimal("1000000.0")
    assert short.liquidation_price == Decimal("426.1797710395")


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


def test_a_position_with_no_entry_price_still_normalizes() -> None:
    """``entryPx`` is the one position field the venue types **optional**, and
    the freeze has to be reserved for responses we cannot read.

    Freezing on a legal response would be the worse failure of the two: it would
    stop *all* healing over a field no comparison depends on — the divergence
    checks read notional, unrealized PnL and margin, each of which is its own
    field. So the absence rides through as ``None`` and the state is still whole.
    """
    state = _fetch_state(_position_without(CROSS_SNAPSHOT, "entryPx"))

    assert state is not None
    (position,) = state.positions
    assert position.entry_price is None
    assert position.notional == Decimal("129.584")


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
        # Not every unreadable body is even an object: an /info error can come
        # back as a bare list or string, which has no key set to name.
        ("a bare list where an object was expected", []),
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


def test_a_flat_account_is_positive_proof_of_flat_not_a_failed_read() -> None:
    """The other side of inv 1, and the one the failure cases cannot stand in for.

    A flat account is not an edge case: the venue omits a coin the account is
    flat in, so an account holding nothing reports ``assetPositions: []`` — the
    first response a live startup barrier ever reads, and the response every
    account gives before its first fill. It must normalize to a *whole* state
    whose ``positions`` is empty, never to the ``None`` that freezes the cycle.
    The account-grain figures still have to arrive with it: an empty book is
    proof about positions, not about cash.
    """
    state = _fetch_state(FLAT_SNAPSHOT)

    assert state is not None
    assert state.positions == ()
    assert state.equity == Decimal("25.9264")
    assert state.free_margin == Decimal("25.9264")
    assert state.cross_maintenance_margin == Decimal("0.0")


def test_a_named_unparseable_read_carries_the_response_shape_not_its_whole_body() -> None:
    """The branch a venue contract change lands on, so it repeats every cycle for
    as long as the contract stays broken — a fifty-position body is kilobytes an
    operator does not need served fifty times over.

    What diagnoses a contract change is the **key set**, so that is what must
    survive the bound; the body follows it truncated, for the value-shaped
    failures a key set cannot show (a figure that is not a number).
    """
    fat = _without(CROSS_SNAPSHOT, "crossMarginSummary") | {
        "assetPositions": CROSS_SNAPSHOT["assetPositions"] * 50
    }

    with capture_events() as events:
        assert _fetch_state(fat) is None

    (failed,) = [e for e in events if e["event"] == NamedEvent.EXCHANGE_REQUEST_FAILED]
    assert "crossMaintenanceMarginUsed" in failed["error"]  # the shape survives
    assert len(failed["error"]) < len(repr(fat)) // 10  # the body does not


# Every venue field name that carries an account or position *quantity*. Not the
# vocabulary a venue response shares generally — ``coin`` and ``time`` appear on
# trades and order records too, and ``szDecimals`` on the instrument meta, so
# naming those elsewhere is not this leak. Four of these are here because reading
# them is the documented mistake rather than because we read them: root
# ``withdrawable`` (ADR-0046 §2), ``rawUsd`` and ``totalRawUsd`` (the cash leg
# net of cost basis, negative for a long), and ``returnOnEquity``.
_QUANTITY_FIELDS = (
    "accountValue",
    "assetPositions",
    "crossMaintenanceMarginUsed",
    "crossMarginSummary",
    "cumFunding",
    "entryPx",
    "liquidationPx",
    "marginSummary",
    "marginUsed",
    "positionValue",
    "rawUsd",
    "returnOnEquity",
    "szi",
    "totalMarginUsed",
    "totalNtlPos",
    "totalRawUsd",
    "unrealizedPnl",
    "withdrawable",
)

_SRC = Path(__file__).parents[3] / "src" / "tickwright"
_OWNER = _SRC / "venues" / "hyperliquid" / "account.py"


def _code_strings(source: str) -> list[str]:
    """Every string literal in ``source`` that is not a docstring.

    Docstrings are excluded because prose is not a read: ``engine/portfolio.py``
    and ``domain/account.py`` both cite ``accountValue`` when explaining where
    the live genesis figure comes from, which is exactly the cross-reference that
    keeps a decision findable. Comments are excluded for free — ``ast`` drops
    them. What is left is the code that would actually reach into a venue
    response.

    The test is *any string used as a statement*, not the canonical
    first-statement docstring: that catches an **attribute** docstring too — the
    bare string under an annotated field, the form ``domain/events.py`` and
    ``domain/protocols.py`` already use — and it costs nothing, because a literal
    that reaches into a venue response is always a subscript, an argument or a
    comparand, never a statement on its own.
    """
    tree = ast.parse(source)
    prose = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in prose
    ]


def test_the_guard_reads_a_docstring_as_prose_wherever_it_sits() -> None:
    """The exclusion has to cover an **attribute** docstring, not just the
    canonical first-statement kind.

    Citing the venue field a quantity is sourced from is the cross-reference the
    guard exists to permit — ``engine/portfolio.py`` and ``domain/account.py``
    both carry one — and ``domain/events.py`` and ``domain/protocols.py``, the
    two modules holding ``VenueAccountState`` and ``fetch_account_state``,
    already use the trailing-string form for other fields. So a field docstring
    naming ``marginSummary.accountValue`` is the likeliest next one written, on
    the likeliest module to write it. Reading it as a leak would fail the build
    over prose and teach the next author to delete the citation — the opposite of
    what this guard is for.
    """
    source = '''"""A module that cites ``accountValue`` in prose alone."""

class VenueAccountState:
    """Its class docstring cites ``marginSummary.accountValue`` too."""

    equity: Decimal
    """The whole account marked to market — ``marginSummary.accountValue``."""
'''

    assert not [literal for literal in _code_strings(source) if "accountValue" in literal]


def test_no_module_outside_this_one_names_a_venue_field_for_these_quantities() -> None:
    """The containment claim the module exists for, enforced rather than asserted
    in a docstring.

    Six field-semantic corrections have landed on this response already, and each
    one was a statement about what a single field means. Concentrating them here
    is what makes a seventh a one-file change instead of a hunt — but only for as
    long as nothing else reaches past the normalizer, and a second reader would
    look like ordinary code review-by-review. ``lint-imports`` cannot see this:
    the leak would be a *string literal* inside a module that is already allowed
    to import this package.

    Honest about its reach: it sees literals, so a field name assembled at
    runtime or held in a variable slips past. It catches the way the mistake
    actually gets made.
    """
    leaks = []
    for module in sorted(_SRC.rglob("*.py")):
        if module == _OWNER:
            continue
        literals = _code_strings(module.read_text(encoding="utf-8"))
        leaks += [
            f"{module.relative_to(_SRC)} names {field!r}"
            for field in _QUANTITY_FIELDS
            if any(field in literal for literal in literals)
        ]

    assert not leaks, (
        "Hyperliquid account/position field names outside venues/hyperliquid/account.py: "
        f"{leaks} — normalize there and pass VenueAccountState/VenuePositionState on"
    )
