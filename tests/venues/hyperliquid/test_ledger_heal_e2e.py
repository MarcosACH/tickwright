"""The Tier-1 heal end to end (issue #178): the real ``LedgerReconciliation``
against the real ``HyperliquidExchange``, over recorded ``clearinghouseState``
bodies.

The suite that drives the heal exhaustively does it against a venue double
(``tests/engine/test_ledger_reconcile.py``), which is the right seam for
behavior and the wrong one for this question: a double answers a
``VenueAccountState`` the case built, so every figure the cycle heals to is one
the test already chose. Here the only fake is the POST transport — the process
boundary (ADR-0022) — so the ``szi``, the entry price and the cash line the
ledger ends up carrying are the venue's own strings, normalised by the adapter
that owns that translation and by nothing in this file.

The two bodies are the recorded pair from the funded testnet account
[#142](https://github.com/MarcosACH/tickwright/issues/142) §2 measured, and the
account identity holds across them: closing the 0.002 BTC realises the −0.034,
so the flat account's 25.9264 of equity is all cash, and reopening it leaves
equity where it was with the cash line 0.034 higher. That is what makes the pair
a *history* rather than two unrelated snapshots — the divergence below is one an
account could actually arrive at.
"""

import asyncio
from decimal import Decimal

from hyperliquid_fakes import TEST_SIGNING_KEY, FakeExchangeApi
from pydantic import SecretStr

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import InstrumentSpec
from tickwright.engine.checkpoint import Checkpointer
from tickwright.engine.ledger_reconcile import (
    DivergenceField,
    DivergenceTier,
    LedgerReconciliation,
)
from tickwright.observability import NamedEvent
from tickwright.observability.testing import capture_events
from tickwright.venues.hyperliquid import (
    HyperliquidConfig,
    HyperliquidExchange,
    HyperliquidUniverse,
)

WALLET_ADDRESS = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"

UNIVERSE = HyperliquidUniverse(
    specs={
        "BTC": InstrumentSpec(
            symbol="BTC", sz_decimals=5, max_decimals=6, min_notional=Decimal("10"), max_sig_figs=5
        )
    },
    asset_indices={"BTC": 3},
)

# The account before the flow this engine never placed: the recorded cross
# snapshot with its one position closed, so the whole balance is cash. Every
# figure is the held snapshot's with the position's contribution removed
# (``test_account.FLAT_SNAPSHOT``, reproduced rather than imported — a fixture
# shared between two files drifts toward whichever one edits it).
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
    "time": 1_730_000_000_000,
    "withdrawable": "25.9264",
}

# The same account holding 0.002 BTC long at 5x, entry 64809, mark 64792.
# Measured (#142 §2): accountValue = totalRawUsd + totalNtlPos = −103.6576 +
# 129.584 = 25.9264, withdrawable = accountValue − totalMarginUsed = 0.0096, and
# crossMaintenanceMarginUsed = 129.584 × 1/(2·40) = 1.6198.
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


def _exchange(snapshot: dict) -> tuple[HyperliquidExchange, FakeExchangeApi]:
    """The real adapter answering ``snapshot``, with the transport the one fake.

    One body per request type is the fake's whole contract — it describes venue
    state, not a call script — so a case that needs the venue to *change* builds
    a second adapter rather than scripting one, which is also the more honest
    shape: two reads a cadence interval apart are two states of one account.
    """
    # ``userAbstraction`` is routed because a **cash heal** re-reads the mode
    # before it writes (ADR-0046 §4), so a body the adapter cannot get is a
    # refused heal rather than a missing fixture — the freeze reaching this
    # suite as a silently unmoved cash line is precisely what it would look
    # like. ``"disabled"`` is what the prescribed remediation produces.
    post = FakeExchangeApi({"clearinghouseState": snapshot, "userAbstraction": "disabled"})
    return (
        HyperliquidExchange(
            config=HyperliquidConfig(
                testnet=True,
                symbols=["BTC"],
                signing_key=SecretStr(TEST_SIGNING_KEY),
                account_address=WALLET_ADDRESS,
            ),
            bus=InMemoryBus(),
            clock=ManualClock(),
            universe=UNIVERSE,
            post=post,
            startup_timeout_seconds=60.0,
        ),
        post,
    )


def test_a_tier_1_divergence_heals_to_the_venues_own_figures_through_the_real_adapter() -> None:
    """Foreign flow, healed end to end, with every figure sourced from the wire.

    The account opens flat through the barrier's own step — ``materialise``, off
    a real body — and the next cadence read finds 0.002 BTC the engine never
    placed. Both Tier-1 grains are adrift at once, which is the ordinary shape
    of a missed fill rather than a contrived one: the size is unaccounted for and
    the cash line behind it is 0.034 stale, that being the open PnL the ledger
    has no position to carry.

    What this asserts past the double-driven suite is **provenance**. ``0.002``
    and ``64809`` are strings in the recorded body above, and the cash line the
    heal lands on is not in it at all — it is ``accountValue − Σ unrealizedPnl``,
    the identity ADR-0040 §7 reads backwards, computed by the adapter from two
    venue fields. A test that handed the cycle a ``VenueAccountState`` would have
    asserted that arithmetic against itself.

    One ``clearinghouseState`` per cycle is asserted on the wire rather than on a
    counter, since the read is ADR-0034's whole venue cost and the snapshot is
    what makes it per-account instead of per-symbol.
    """
    store = SQLiteStore(":memory:")
    opening, _ = _exchange(FLAT_SNAPSHOT)
    keeper = Checkpointer(spec=opening.account_spec(), store=store, clock=ManualClock(7))
    keeper.recover()
    assert asyncio.run(
        LedgerReconciliation(exchange=opening, checkpointer=keeper).materialise_account()
    )

    venue, post = _exchange(CROSS_SNAPSHOT)
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)

    with capture_events() as logs:
        divergences = asyncio.run(cycle.reconcile_account())

    assert [(d.tier, d.field, d.symbol, d.ledger, d.venue) for d in divergences or ()] == [
        (DivergenceTier.TIER_1, DivergenceField.CASH, None, Decimal("25.9264"), Decimal("25.9604")),
        (
            DivergenceTier.TIER_1,
            DivergenceField.SIGNED_SIZE,
            "BTC",
            Decimal("0"),
            Decimal("0.002"),
        ),
        # The same missed fill a third time, in the unit free margin is computed
        # in: a ledger holding no position posts no margin behind one, so its
        # free margin is its whole cash line, against a venue that has 25.9168
        # of it locked up. Off the wire like the two above — the venue's side is
        # ``crossMarginSummary``'s own pair of fields, differenced by the
        # adapter (ADR-0046 §2).
        (
            DivergenceTier.TIER_2,
            DivergenceField.FREE_MARGIN,
            None,
            Decimal("25.9264"),
            Decimal("0.0096"),
        ),
    ]

    ledger = keeper.portfolio
    healed = ledger.position("BTC", strategy_id=None)
    assert healed is not None
    assert healed.size == Decimal("0.002")
    assert healed.entry_price == Decimal("64809.0")
    assert ledger.account().cash == Decimal("25.9604")

    # The record, and the durable half behind it. The roster is asserted whole
    # rather than indexed into: the unattributed partition being *the* one the
    # pass created is what says the heal reached for no strategy, and a lookup
    # by key would pass just as well beside one it had.
    assert [
        (log["field"], log["symbol"], log["event_id"])
        for log in logs
        if log["event"] == NamedEvent.ACCOUNT_HEALED.value
    ] == [("signed_size", "BTC", "reconcile:BTC:7"), ("cash", None, "reconcile:cash:7")]
    assert [(p.strategy_id, p.symbol, p.signed_size) for p in store.all_positions()] == [
        (None, "BTC", Decimal("0.002"))
    ]
    restored = store.load_account()
    assert restored is not None
    assert restored.account_id == opening.account_spec().account_id
    assert restored.cash == Decimal("25.9604")

    # The mode read is second and there is one of it: it is the guard on the
    # write, so it follows the anchor it guards and is asked only because this
    # pass found a cash line to heal (ADR-0046 §4 buys its affordability that
    # way — ``userAbstraction`` is weight 20 against the anchor's 2).
    assert [payload["type"] for _, payload in post.requests] == [
        "clearinghouseState",
        "userAbstraction",
    ]
