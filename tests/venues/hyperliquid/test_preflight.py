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
from tickwright.domain import (
    InstrumentSpec,
    LeverageBook,
    LeverageOutOfBounds,
    LeverageSpec,
    VenueAccountModeUnsupported,
)
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


def test_connecting_makes_the_mode_read_and_no_other_venue_request() -> None:
    """``start()`` is ADR-0024 step 4, and the gate opens it (ADR-0046 §3).

    Two claims in one recorded exchange. The mode read **is** the step's first
    venue traffic — it gates everything after it, because a wrong mode
    invalidates the premise the leverage push's own check would reason from, so
    reporting mismatches computed against a margin model that does not apply
    would be noise on top of an error. And it is so far the step's **only**
    traffic: the fake routes nothing else, so the leverage push arriving behind
    it (ADR-0044 §7) must rewrite this test rather than silently survive it.

    The adapter still holds no connection of its own — every request is scoped
    to the call that makes it — so this is the whole of what connecting does."""
    post = FakeExchangeApi({"userAbstraction": "default"})

    asyncio.run(_exchange(post).start())

    assert [query["type"] for (_url, query) in post.requests] == ["userAbstraction"]


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
        leverage=LeverageBook({"BTC": LeverageSpec(mode="cross", leverage=50)}),
    )

    with pytest.raises(LeverageOutOfBounds) as refusal:
        asyncio.run(exchange.start())

    assert "BTC" in str(refusal.value)
    assert "40" in str(refusal.value)
