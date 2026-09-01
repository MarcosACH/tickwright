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
from hyperliquid_fakes import TEST_SIGNING_KEY, FakeExchangeApi, request_type
from pydantic import SecretStr

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.domain import (
    EMPTY_LEVERAGE_BOOK,
    InstrumentSpec,
    LeverageBook,
    LeverageOutOfBounds,
    LeverageSpec,
    VenueAccountModeUnsupported,
    VenueLeverageMismatch,
    VenueLeveragePushFailed,
)
from tickwright.observability import NamedEvent
from tickwright.observability.testing import capture_events
from tickwright.venues.hyperliquid import (
    HyperliquidConfig,
    HyperliquidExchange,
    HyperliquidUniverse,
)

UNIVERSE = HyperliquidUniverse(
    specs={
        "BTC": InstrumentSpec(
            symbol="BTC",
            sz_decimals=5,
            max_decimals=6,
            min_notional=Decimal("10"),
            max_sig_figs=5,
            # The venue's own cap for BTC perps, so the §9 bound these tests run
            # behind passes on the leverages they configure rather than tripping
            # on ``InstrumentSpec``'s conservative 1x default.
            max_leverage=40,
        ),
        "ETH": InstrumentSpec(
            symbol="ETH",
            sz_decimals=4,
            max_decimals=6,
            min_notional=Decimal("10"),
            max_sig_figs=5,
            max_leverage=25,
        ),
    },
    asset_indices={"BTC": 3, "ETH": 1},
)

STARTUP_TIMEOUT_SECONDS = 60.0
"""The barrier budget the composition root hands the adapter (ADR-0044 §6): the
mode read is bounded by ``startup_reconciliation_timeout``, never a second one."""

OK_ENVELOPE: dict = {"status": "ok", "response": {"type": "default"}}
"""What ``updateLeverage`` answers — measured in #142, and the reason the push
has no no-op branch to write: pushing ``cross/20x`` onto a symbol already at
``cross/20x`` returns **this same envelope** as a real change, so ``ok`` ⇒
success is the whole taxonomy (ADR-0044 §6's correction)."""


def _state(*positions: dict) -> dict:
    """A ``clearinghouseState`` body holding ``positions``.

    The envelope figures are the recorded flat snapshot's (``test_account.py``,
    measured in #142); the push reads only ``assetPositions[].position.{coin,
    leverage}``, so they are here to keep the body the venue's shape rather than
    because anything under test looks at them.
    """
    return {
        "assetPositions": list(positions),
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


def _held(coin: str, *, mode: str, leverage: int) -> dict:
    """One ``assetPositions`` row for a coin the account holds.

    Shaped on the recorded cross snapshot; ``leverage.{type, value}`` is the
    pair the push compares against config, and is the only part a test varies.
    """
    return {
        "type": "oneWay",
        "position": {
            "coin": coin,
            "szi": "0.002",
            "entryPx": "64809.0",
            "positionValue": "129.584",
            "unrealizedPnl": "-0.034",
            "returnOnEquity": "-0.0026231",
            "marginUsed": "25.9168",
            "liquidationPx": None,
            "maxLeverage": 40,
            "leverage": {"type": mode, "value": leverage},
            "cumFunding": {"allTime": "0.0", "sinceOpen": "0.0", "sinceChange": "0.0"},
        },
    }


class _FlakyApi(FakeExchangeApi):
    """A venue that is down for its first ``failures`` requests, then answers.

    The one thing ``FakeExchangeApi`` deliberately will not do: it answers by
    *what a request asks*, never by call order, so that a test describes venue
    state rather than a call script. An outage that clears **is** a call script
    — it is the passage of time that the retry is about — so it gets a fake of
    its own here rather than bending the shared one into a scripted mode every
    other test would then have to reason about.
    """

    def __init__(self, *, failures: int, then: dict[str, object], error: OSError) -> None:
        super().__init__(then)
        self._remaining_failures = failures
        self._error = error

    async def __call__(self, url: str, payload: dict) -> object:
        if self._remaining_failures:
            self._remaining_failures -= 1
            self.requests.append((url, payload))
            raise self._error
        return await super().__call__(url, payload)


def _exchange(
    post: FakeExchangeApi,
    *,
    clock: ManualClock | None = None,
    account_address: str | None = None,
    leverage: LeverageBook = EMPTY_LEVERAGE_BOOK,
) -> HyperliquidExchange:
    return HyperliquidExchange(
        config=HyperliquidConfig(
            testnet=True,
            symbols=["BTC"],
            signing_key=SecretStr(TEST_SIGNING_KEY),
            account_address=account_address,
        ),
        bus=InMemoryBus(),
        clock=clock if clock is not None else ManualClock(),
        universe=UNIVERSE,
        post=post,
        startup_timeout_seconds=STARTUP_TIMEOUT_SECONDS,
        leverage=leverage,
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


@pytest.mark.parametrize("mode", ["default", "disabled"])
def test_both_manual_standard_literals_start_normally(mode: str) -> None:
    """The allowlist has **two** entries, and the second is why it is a list.

    ``"default"`` is the *unset* state — an account that never touched the
    setting — and ``"disabled"`` is what ``userSetAbstraction("disabled")``
    produces. The remediation this gate prints moves an account to the
    **second**, so the obvious ``mode == "default"`` check would refuse the very
    account the operator was just told to build. Both were measured running real
    perp positions with the perps clearinghouse fully populated (ADR-0046 §3),
    which is why neither is merely tolerated.

    Parametrized rather than folded into one call so a regression that accepted
    only one of them fails on the literal it dropped, by name."""
    exchange = _exchange(FakeExchangeApi({"userAbstraction": mode}))

    asyncio.run(exchange.start())  # no refusal is the assertion


def test_a_mode_that_cannot_be_read_refuses_to_start_once_the_budget_is_spent() -> None:
    """The failure that must **not** be answered with the good case (ADR-0046 §3).

    A boot-time venue blip is real, so the read is retried — but "assume
    Manual/Standard on error" is exactly the assumption every account-grain
    number downstream would then be computed against, and at boot there is
    nothing correct to fall back to. So the budget runs out and the engine
    refuses, which is ADR-0011 invariant 1's freeze-don't-guess applied to a
    precondition.

    Three claims, and the middle one is the reason this is not just the tracer
    again: it **retried** rather than refusing on the first failure, it stayed
    inside ADR-0024's budget rather than minting its own, and it refused rather
    than proceeding. The clock is virtual, so the whole 60 s window costs the
    suite nothing.

    The elapsed bound is the backoff **cap** stated as an assertion: capped
    doubling can overshoot the deadline by at most one full interval, where an
    uncapped one would sail far past it — a 60 s budget faulting at ~120 s is
    the failure mode the cap exists to prevent."""
    clock = ManualClock(start_ns=0)
    post = FakeExchangeApi({"userAbstraction": ConnectionError("venue unreachable")})

    with pytest.raises(VenueAccountModeUnsupported) as refusal:
        asyncio.run(_exchange(post, clock=clock).start())

    assert len(post.requests) > 1, "a single attempt is not a bounded retry"
    elapsed_seconds = clock.timestamp_ns() / 1_000_000_000
    assert STARTUP_TIMEOUT_SECONDS <= elapsed_seconds < STARTUP_TIMEOUT_SECONDS + 30
    message = str(refusal.value)
    assert "venue unreachable" in message, "the operator needs the underlying failure"
    assert "default" in message and "disabled" in message


def test_a_boot_time_blip_that_clears_inside_the_budget_starts_normally() -> None:
    """The other half of the retry, and the reason there is one at all.

    A gate that refused on the first refused connection would turn every
    boot-time venue blip into a crash-loop against the supervisor, which is the
    outcome ADR-0024's bounded window exists to avoid — a transient failure
    resolves and startup proceeds; only a sustained one exits non-zero for the
    supervisor to back off.

    Asserted on the *cleared* boot rather than on the attempt count, so it
    describes what an operator sees: two failed reads, then a running engine."""
    clock = ManualClock(start_ns=0)
    post = _FlakyApi(
        failures=2, then={"userAbstraction": "disabled"}, error=TimeoutError("venue timed out")
    )

    asyncio.run(_exchange(post, clock=clock).start())  # no refusal is the assertion

    assert len(post.requests) == 3, "the two failures and the read that cleared"
    assert clock.timestamp_ns() < STARTUP_TIMEOUT_SECONDS * 1_000_000_000


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"userAbstraction": "disabled"}, id="wrapped-in-an-object"),
        pytest.param(None, id="null"),
        pytest.param(0, id="re-typed"),
    ],
)
def test_a_body_that_is_not_a_mode_literal_is_a_failed_read_not_a_refused_mode(
    body: object,
) -> None:
    """The venue answers this query with a **bare JSON string** (#152 measured
    ``'disabled'``), so anything else is the venue changing its contract — the
    same "we are not reading what we think we are" a missing field means
    elsewhere in this adapter, and a failed read for the same reason.

    It matters which of the two failures this is, because they end differently.
    An unrecognised *literal* is the venue telling us a mode we refuse, and the
    operator can act on it; a body that is not a literal tells us nothing about
    the mode at all, so it takes the retry — and must **not** print the
    remediation, which would send an operator to run ``userSetAbstraction`` on
    an account whose mode was never actually in question."""
    clock = ManualClock(start_ns=0)
    post = FakeExchangeApi({"userAbstraction": body})

    with pytest.raises(VenueAccountModeUnsupported) as refusal:
        asyncio.run(_exchange(post, clock=clock).start())

    assert len(post.requests) > 1, "an unreadable body is retried, not refused outright"
    message = str(refusal.value)
    assert repr(body) in message, "the operator needs the body we could not read"
    assert "userSetAbstraction" not in message, "there is no mode here to remediate"


def test_a_mode_literal_the_venue_has_not_shipped_yet_refuses_on_the_first_read() -> None:
    """Allowlist, not denylist: a fifth literal refuses rather than passing.

    A mode nobody has measured is the worst possible case in which to guess, and
    guessing "it is probably fine" is precisely what a denylist of the two known
    pooled modes would do the day the venue ships a third.

    It refuses on the **first** read, spending none of the budget, because this
    is not a failed read — the venue answered, clearly, with a mode. Retrying
    would re-ask a question already answered and delay the operator's error by
    the whole startup window, and the clock assertion is what pins that."""
    clock = ManualClock(start_ns=0)
    post = FakeExchangeApi({"userAbstraction": "someNewMode"})

    with pytest.raises(VenueAccountModeUnsupported) as refusal:
        asyncio.run(_exchange(post, clock=clock).start())

    assert len(post.requests) == 1
    assert clock.timestamp_ns() == 0, "an answered question consumes no backoff"
    assert "someNewMode" in str(refusal.value)


MASTER_ACCOUNT = "0x049d0000000000000000000000000000000015F76"


def test_the_gate_classifies_the_trading_account_not_the_agent_wallet() -> None:
    """The mode is a property of the account the **ledger** is bound to.

    With an API/agent wallet the signing key's own address and the account it
    acts for are different addresses, and the agent's is its own empty account
    — #152 measured the venue treating it exactly that way, refusing a
    user-signed action addressed to the master with *"Must deposit before
    performing actions"* against the **signer's** address. So a gate that
    classified the key's address would read the mode of an account that holds
    nothing and trades nothing, and wave through a master account in any mode
    at all.

    Asserted on the outbound query rather than on the outcome, because the
    outcome is identical either way on a single-wallet deployment — which is
    what would let this regress unnoticed."""
    post = FakeExchangeApi({"userAbstraction": "disabled"})
    exchange = _exchange(post, account_address=MASTER_ACCOUNT)

    asyncio.run(exchange.start())

    ((_url, query),) = post.requests
    assert query == {"type": "userAbstraction", "user": MASTER_ACCOUNT}


def test_a_run_with_nothing_traded_makes_the_mode_read_and_no_other_venue_request() -> None:
    """``start()`` is ADR-0024 step 4, and the gate opens it (ADR-0046 §3).

    The mode read **is** the step's first venue traffic — it gates everything
    after it, because a wrong mode invalidates the premise the leverage push's
    own check reasons from, so reporting mismatches computed against a margin
    model that does not apply would be noise on top of an error.

    An **empty** book is what makes it the only traffic, and it is a legitimate
    value rather than an unresolved one: a run with nothing traded has no symbol
    to align, so there is nothing to read the account for either. The push's own
    traffic is asserted by the test below, against a book with a symbol in it.

    The adapter holds no connection of its own — every request is scoped to the
    call that makes it — so this is the whole of what connecting does."""
    post = FakeExchangeApi({"userAbstraction": "default"})

    asyncio.run(_exchange(post).start())

    assert [query["type"] for (_url, query) in post.requests] == ["userAbstraction"]


def test_a_symbol_the_account_is_flat_in_is_written_blind_behind_the_mode_gate() -> None:
    """The push's ordinary case (ADR-0044 §4): no position, so write.

    **The risky symbols are exactly the ones holding a position**, and a symbol
    with nothing open has nothing to re-margin and nothing to reject on position
    grounds — so the write needs no prior knowledge of the venue's stored
    setting, and the design pays one ``clearinghouseState`` read for the whole
    boot rather than one ``activeAssetData`` read per symbol (§4's declined
    option, left a cost trade by #142 rather than an unknown).

    Three claims, and the ordering is the load-bearing one. The mode gate goes
    first, because the push's own three-way split is computed against a margin
    model a pooled account invalidates; the account read comes next, being what
    the split needs; the signed write comes last. The wire is asserted in full —
    ``asset`` is the venue's index, not the symbol, and ``isCross`` and
    ``leverage`` are one value in one action, which is why config carries them
    as one ``LeverageSpec`` (§2).
    """
    post = FakeExchangeApi(
        {
            "userAbstraction": "disabled",
            "clearinghouseState": _state(),
            "updateLeverage": OK_ENVELOPE,
        }
    )
    book = LeverageBook(entries={"BTC": LeverageSpec(mode="cross", leverage=10)})

    asyncio.run(_exchange(post, leverage=book).start())

    assert [request_type(url, payload) for (url, payload) in post.requests] == [
        "userAbstraction",
        "clearinghouseState",
        "updateLeverage",
    ]
    (_url, sent) = post.requests[-1]
    assert sent["action"] == {
        "type": "updateLeverage",
        "asset": 3,
        "isCross": True,
        "leverage": 10,
    }


def test_a_traded_symbol_nobody_configured_is_pushed_at_the_safe_default() -> None:
    """Scope is every strategy-traded symbol, the **defaulted ones included**
    (ADR-0044 §3) — not the perp universe, and not the feed list.

    This is the asymmetric one. Pushing a symbol nobody configured costs one
    write; *not* pushing it leaves the venue holding whatever leverage the
    account was last set to while the model computes the position's margin at
    ``1x`` full notional — so the engine would report more collateral behind a
    levered position than the venue is actually holding, **understating** risk
    in the one direction that matters. An unconfigured symbol is therefore a
    complete conservative specification (``1x``/``isolated``, ADR-0040 §5), not
    a hole to skip.

    ``resolve`` is what makes that true, so the book handed over here is built
    the way the composition root builds it — sparse config completed over the
    traded set — rather than written out complete, which would assert the
    resolution's output against itself. The expected pair is ADR-0040 §5's
    literal safest combination and the venue's own asset index, neither of them
    re-derived from the code under test.
    """
    post = FakeExchangeApi(
        {
            "userAbstraction": "disabled",
            "clearinghouseState": _state(),
            "updateLeverage": OK_ENVELOPE,
        }
    )
    book = LeverageBook.resolve(
        {"BTC": LeverageSpec(mode="cross", leverage=10)}, traded=["BTC", "ETH"]
    )

    asyncio.run(_exchange(post, leverage=book).start())

    pushed = [payload["action"] for (url, payload) in post.requests if url.endswith("/exchange")]
    assert {"type": "updateLeverage", "asset": 1, "isCross": False, "leverage": 1} in pushed
    assert len(pushed) == 2, "the configured symbol is pushed too, not replaced by the default"


def test_a_held_position_the_venue_already_agrees_with_is_left_alone_and_named() -> None:
    """The skip arm of the three-way split (ADR-0044 §4): aligned → no write.

    Skipping is not an optimisation here, it is the safe branch. The write the
    account read exists to avoid is a re-margin of a *held* position, and the
    one symbol where a needless ``updateLeverage`` could move real collateral is
    exactly the one already carrying risk — so agreement has to be established
    before the boot writes anything, not asserted afterwards from the response.

    Which is why the skip branch is the sole source of
    ``EXCHANGE_LEVERAGE_UNCHANGED``. A no-op push and a real change come back as
    the **identical** ``ok`` envelope (measured in #142, ADR-0044 §6's
    correction), so the write path cannot tell an operator that nothing moved;
    only the branch that declined to write knows it. The expected pair is the
    configured one, and the venue body says the same thing in the venue's own
    vocabulary (``leverage.{type, value}``) rather than in config's.
    """
    post = FakeExchangeApi(
        {
            "userAbstraction": "disabled",
            "clearinghouseState": _state(_held("BTC", mode="cross", leverage=10)),
            "updateLeverage": OK_ENVELOPE,
        }
    )
    book = LeverageBook(entries={"BTC": LeverageSpec(mode="cross", leverage=10)})

    with capture_events() as logs:
        asyncio.run(_exchange(post, leverage=book).start())

    assert [request_type(url, payload) for (url, payload) in post.requests] == [
        "userAbstraction",
        "clearinghouseState",
    ], "an aligned held position is never written to"
    unchanged = [
        (log["symbol"], log["mode"], log["leverage"])
        for log in logs
        if log["event"] == NamedEvent.EXCHANGE_LEVERAGE_UNCHANGED
    ]
    assert unchanged == [("BTC", "cross", 10)]


def test_a_held_position_at_a_mode_the_venue_has_never_reported_is_a_failed_read() -> None:
    """The branch ``held_leverage``'s refusal exists to prevent, pinned.

    ``LeverageSpec`` is a plain frozen dataclass, so an unrecognised
    ``leverage.type`` trusted into it would compare unequal to every configured
    spec — and the boot would read *the venue changing its contract* as a held
    disagreement, raising ``VenueLeverageMismatch`` and sending an operator to
    the venue UI to fix a leverage that was never wrong. It is refused at the
    read instead, which makes it an unreadable body like any other: retried
    under the shared boot budget, then ``VenueLeveragePushFailed``.

    Asserted as **not** ``VenueLeverageMismatch`` rather than only as the type
    raised, because the mismatch is the plausible wrong answer here and a type
    check alone would pass under it if the two errors were ever merged.

    Covers the gate from ``held_leverage``'s side. ``test_account.py`` covers it
    from ``_isolated_collateral``'s, and since #180's architecture pass both go
    through one ``reported_margin_mode`` — two readers of one field that used to
    decide validity separately, one derived from ``MarginMode`` and one not.
    """
    post = FakeExchangeApi(
        {
            "userAbstraction": "disabled",
            "clearinghouseState": _state(_held("BTC", mode="portfolioMargin", leverage=10)),
            "updateLeverage": OK_ENVELOPE,
        }
    )
    book = LeverageBook(entries={"BTC": LeverageSpec(mode="cross", leverage=10)})

    with pytest.raises(VenueLeveragePushFailed) as refusal:
        asyncio.run(_exchange(post, clock=ManualClock(start_ns=0), leverage=book).start())

    assert not isinstance(refusal.value, VenueLeverageMismatch), (
        "a venue contract change is not a disagreement about a held position"
    )
    assert "portfolioMargin" in str(refusal.value), "the operator needs the body that was refused"
    assert "updateLeverage" not in [
        request_type(url, payload) for (url, payload) in post.requests
    ], "a book that could not be read aligns nothing"


def test_held_positions_the_venue_disagrees_about_refuse_to_start_naming_all_of_them() -> None:
    """The refuse arm of the three-way split (ADR-0044 §5), and the reason the
    account read exists at all.

    Config wins at startup — but not over a position that is already open. The
    venue would accept the write, and accepting it would silently re-margin
    live risk: an operator who lowered BTC in the venue UI to de-risk would
    find the boot quietly putting it back. So a disagreement about a *held*
    symbol is a refusal, not a push, and the two symbols here disagree in the
    two different ways one can — a leverage that differs and a mode that does.

    **Every** disagreement is named in one error, the venue twin of
    ``StoreAccountMismatch``: reporting one per restart makes an operator
    discover a two-symbol drift by rebooting twice, and the second reboot is the
    one that happens with the market moving. Both pairs are printed per symbol
    because either side may be the wrong one — the fix is sometimes config and
    sometimes the venue UI, and an error naming only what was configured cannot
    say which.

    Nothing is written on the way to the refusal. The disagreements are
    collected across the whole book before it raises, so a boot that is going to
    refuse never leaves half the account re-margined behind it.
    """
    post = FakeExchangeApi(
        {
            "userAbstraction": "disabled",
            "clearinghouseState": _state(
                _held("BTC", mode="cross", leverage=10),
                _held("ETH", mode="isolated", leverage=5),
            ),
            "updateLeverage": OK_ENVELOPE,
        }
    )
    book = LeverageBook(
        entries={
            "BTC": LeverageSpec(mode="cross", leverage=20),
            "ETH": LeverageSpec(mode="cross", leverage=5),
        }
    )

    with pytest.raises(VenueLeverageMismatch) as refusal:
        asyncio.run(_exchange(post, leverage=book).start())

    assert "BTC" in str(refusal.value) and "cross 20x" in str(refusal.value)
    assert "cross 10x" in str(refusal.value)
    assert "ETH" in str(refusal.value) and "isolated 5x" in str(refusal.value)
    assert [request_type(url, payload) for (url, payload) in post.requests] == [
        "userAbstraction",
        "clearinghouseState",
    ], "a boot that refuses re-margins nothing on the way there"


def test_an_account_mode_the_gate_refuses_never_reaches_the_leverage_push() -> None:
    """The gate is **ahead of** the push and gates it (ADR-0046 §3, ADR-0024
    step 4) — asserted here as the absence of a signed write.

    Distinct from the gate's own tests, which run an empty book and so have no
    write to suppress: the book here holds a symbol the account is flat in, which
    is precisely the case the push writes **blind**. So this fails the moment the
    two guards are reordered, which the gate's tests would not notice.

    Why the ordering is load-bearing in this direction: under a pooled mode the
    perps clearinghouse is a sub-ledger, so the three-way split would be computed
    against positions that are not the account's — and the branch that writes is
    the one that acts on a *missing* position. A pooled account reads flat in the
    exact place a blind write is issued, so a push that ran first would re-margin
    symbols on the strength of a read that never described them.
    """
    post = FakeExchangeApi(
        {
            "userAbstraction": "unifiedAccount",
            "clearinghouseState": _state(),
            "updateLeverage": OK_ENVELOPE,
        }
    )
    book = LeverageBook(entries={"BTC": LeverageSpec(mode="cross", leverage=10)})

    with pytest.raises(VenueAccountModeUnsupported):
        asyncio.run(_exchange(post, leverage=book).start())

    assert [request_type(url, payload) for (url, payload) in post.requests] == ["userAbstraction"]


def test_a_write_that_never_lands_retries_and_faults_inside_the_shared_boot_budget() -> None:
    """One budget covers **both** guards (ADR-0044 §6, ADR-0043 §6's precedent).

    The push runs in the same boot window as the mode gate and the barrier, and
    faces the same transient-blip reality, so it reuses
    ``startup_reconciliation_timeout`` rather than minting a second timeout. That
    is a claim about the *whole boot*, not about the push in isolation: a push
    that started a fresh 60 s window of its own would still retry, still fault,
    and still look right in every assertion below except the elapsed one — while
    a boot the operator budgeted a minute for took two.

    So the gate is made to spend most of the budget first (five refused reads,
    31 s of capped doubling) and the write then fails for good. Under one shared
    deadline the boot faults at ~62 s; under two it would fault at ~92 s, past
    the bound asserted here. The clock is virtual, so neither costs the suite
    anything.

    ``VenueLeveragePushFailed`` rather than ``VenueLeverageMismatch``: this is a
    write that never landed, not a disagreement about a held position — the same
    split the mode gate keeps between an unreadable mode and a refused one. The
    operator needs the symbol left unaligned and the underlying failure, because
    the account is now in neither the configured state nor a known one.
    """
    clock = ManualClock(start_ns=0)
    post = _FlakyApi(
        failures=5,
        then={
            "userAbstraction": "disabled",
            "clearinghouseState": _state(),
            "updateLeverage": ConnectionError("venue unreachable"),
        },
        error=TimeoutError("venue timed out"),
    )
    book = LeverageBook(entries={"BTC": LeverageSpec(mode="cross", leverage=10)})

    with pytest.raises(VenueLeveragePushFailed) as refusal:
        asyncio.run(_exchange(post, clock=clock, leverage=book).start())

    writes = [
        1 for (url, payload) in post.requests if request_type(url, payload) == "updateLeverage"
    ]
    assert len(writes) > 1, "a single attempt is not a bounded retry"
    elapsed_seconds = clock.timestamp_ns() / 1_000_000_000
    assert STARTUP_TIMEOUT_SECONDS <= elapsed_seconds < STARTUP_TIMEOUT_SECONDS + 30, (
        "the push shares the gate's deadline; a second budget would fault at ~2x"
    )
    message = str(refusal.value)
    assert "BTC" in message, "the operator needs the symbol left unaligned"
    assert "venue unreachable" in message, "and the underlying failure"


@pytest.mark.parametrize(
    "rejection",
    [
        "Invalid leverage value",
        (
            "Isolated position does not have sufficient margin available to decrease "
            "leverage. To decrease leverage, add margin to the position."
        ),
        "Cannot switch leverage type with open position.",
    ],
)
def test_a_refused_write_faults_at_once_quoting_the_venue_s_own_string(rejection: str) -> None:
    """The other half of the taxonomy (ADR-0044 §6 as corrected by #142): the
    venue returns its refusal as a **value**, not an exception.

    ``{"status": "err", "response": "<plain string>"}`` — a bare string in a
    200-OK body, so an adapter that only caught exceptions would read a refused
    write as a completed one and clear startup against an account it never
    aligned. Inspecting the envelope is the whole point, and ``status == "ok"``
    is the sole success: a no-op push and a real change return the *identical*
    ``ok`` envelope, so there is no third outcome to tolerate.

    It faults **at once**, which is the claim the elapsed assertion pins. The
    transport failures above are transient by assumption and get the budget;
    a refusal is the venue answering, and re-asking cannot change its mind
    inside a boot window. Worse, the retry would re-send a *signed write*
    against a live account once a second — so a refusal reaching the OSError
    branch is not a slow error, it is a write storm.

    The three strings are the ones ADR-0044 §6 recorded against the real venue,
    parametrized rather than merged because they arrive on different causes and
    a regression that special-cased any one of them into a retry — or into
    ``VenueLeverageMismatch``, whose remedy is a different place entirely —
    fails on the string it mishandled, by name. The engine deliberately does
    **not** re-classify them: the venue's own sentence is what the operator can
    act on, and a taxonomy over it would be a second encoding of a venue fact.
    """
    clock = ManualClock(start_ns=0)
    post = FakeExchangeApi(
        {
            "userAbstraction": "disabled",
            "clearinghouseState": _state(),
            "updateLeverage": {"status": "err", "response": rejection},
        }
    )
    book = LeverageBook(entries={"BTC": LeverageSpec(mode="cross", leverage=10)})

    with pytest.raises(VenueLeveragePushFailed) as refusal:
        asyncio.run(_exchange(post, clock=clock, leverage=book).start())

    writes = [
        1 for (url, payload) in post.requests if request_type(url, payload) == "updateLeverage"
    ]
    assert writes == [1], "a refusal is answered, not re-sent — retrying signs a write storm"
    assert clock.timestamp_ns() == 0, "an answered question consumes no backoff"
    message = str(refusal.value)
    assert rejection in message, "the venue's own sentence is what the operator can act on"
    assert "BTC" in message, "and the symbol left unaligned"


def test_a_leverage_above_the_venue_cap_refuses_to_start_on_live_too() -> None:
    """The identical bound, refused identically on the other path (ADR-0044 §9).

    The venue does enforce this itself — but only at the instant a position is
    opened, and only on live, which is exactly why the check cannot be left to
    it: paper would accept the same impossible value silently and compute a
    whole margin model off it. One shared ``domain`` check refuses both paths
    with one message, so the two cannot drift apart.

    The mode gate runs **first** and this runs behind it (ADR-0046 §3): a pooled
    account invalidates the premise the margin model reasons from, so a leverage
    complaint computed against a model that does not apply would be noise on top
    of an error. The healthy ``"disabled"`` mode here is what lets this one be
    reached at all.
    """
    exchange = HyperliquidExchange(
        config=HyperliquidConfig(
            testnet=True, symbols=["BTC"], signing_key=SecretStr(TEST_SIGNING_KEY)
        ),
        bus=InMemoryBus(),
        clock=ManualClock(),
        universe=HyperliquidUniverse(
            specs={
                "BTC": InstrumentSpec(
                    symbol="BTC",
                    sz_decimals=5,
                    max_decimals=6,
                    min_notional=Decimal("10"),
                    max_sig_figs=5,
                    max_leverage=40,
                )
            },
            asset_indices={"BTC": 3},
        ),
        post=FakeExchangeApi({"userAbstraction": "disabled"}),
        startup_timeout_seconds=STARTUP_TIMEOUT_SECONDS,
        leverage=LeverageBook(entries={"BTC": LeverageSpec(mode="cross", leverage=50)}),
    )

    with pytest.raises(LeverageOutOfBounds) as refusal:
        asyncio.run(exchange.start())

    assert "BTC" in str(refusal.value)
    assert "40" in str(refusal.value)
