"""The boot guards — what must be true of the venue account before this process
places anything (ADR-0046 §3).

The account abstraction-mode gate runs **first** among them and gates everything
after it. It is a precondition rather than a reconciliation: nothing here
compares a recorded value against an observed one, because under a pooled mode
there is no observed value worth comparing. The perps clearinghouse stops being
the account boundary and becomes a sub-ledger of it, so equity and free margin
come back an order of magnitude low with nothing in the response saying so
(ADR-0046 §1). A wrong mode does not make one number wrong; it makes every
account-grain number mean something else.

Isolated here rather than inlined into the adapter because both guards are
refusals that must fire before the barrier and before any order, and both fail
**closed** — which is testable against recorded responses only if it is reachable
without going near the order path.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from tickwright.domain import Clock, VenueAccountModeUnsupported

InfoRead = Callable[[dict[str, Any]], Awaitable[object]]
"""One unsigned ``POST /info`` query, as the adapter makes it."""

SUPPORTED_ACCOUNT_MODES = frozenset({"default", "disabled"})
"""The two ``userAbstraction`` literals that mean Manual/Standard.

An **allowlist**, never a denylist: a literal the venue adds later refuses, since
a mode we have never seen is the worst possible one to guess about.

Two entries, not one, and the second is the load-bearing one. ``"default"`` is
the *unset* state — an account that never touched the setting — while the
remediation this module's error prescribes moves an account to ``"disabled"``.
So the obvious ``mode == "default"`` check would refuse the very account the
operator was just told to build. Both were measured running real perp positions
with the perps clearinghouse fully populated (ADR-0046 §3).
"""


_RETRY_INITIAL_BACKOFF_SECONDS = 1.0
_RETRY_MAX_BACKOFF_SECONDS = 30.0
"""``StartupBarrier.run``'s own pacing, matched deliberately.

This read cannot *be* a barrier step — it has to clear before the barrier's own
venue reads run, and ``venues`` may not import ``engine`` (ADR-0032) — so it
repeats the policy rather than sharing the mechanism, under the same budget the
barrier is given (ADR-0044 §6: one boot-time budget, never a second timeout).

The cap is what keeps an uncapped doubling from carrying the clock far past the
deadline: without it a large budget would refuse nearly a whole interval late,
making real time-to-``FAULTED`` up to ~2× the configured window."""

_NS_PER_SECOND = 1_000_000_000


async def verify_account_mode(
    *, info: InfoRead, address: str, clock: Clock, timeout_seconds: float
) -> None:
    """Refuse to start unless ``address`` is in a supported account mode.

    Returns on the two supported literals and raises on everything else. There
    is no third outcome — in particular there is no "assume standard on error",
    which is ADR-0011 invariant 1's freeze-don't-guess applied to a
    precondition. At boot there is nothing correct to fall back **to**: every
    account-grain number the engine is about to read depends on this answer.

    The read is one unsigned ``userAbstraction`` query, and the venue answers it
    with a **bare JSON string** — measured, not inferred: the pinned SDK types
    the mode as ``Literal["unifiedAccount", "portfolioMargin", "disabled"]`` and
    carries no response shape at all, ``"default"`` appearing only on the read
    because it is the unset state.

    An **unreadable** mode may be a boot-time blip, so it is retried with capped
    backoff until ``timeout_seconds`` is spent and only then refuses. Slept on
    the injected ``Clock``, so a venue that is down cannot be hammered and
    virtual time carries the whole window for free under ``ManualClock``.

    An **unrecognised literal** is not a failed read and takes no retry: the
    venue answered, and re-asking cannot change a deployment fact inside a boot
    window — it would only delay the remediation the operator needs.
    """
    deadline_ns = clock.timestamp_ns() + int(timeout_seconds * _NS_PER_SECOND)
    backoff_seconds = _RETRY_INITIAL_BACKOFF_SECONDS
    while True:
        try:
            mode = await _read_account_mode(info, address=address)
        except (OSError, ValueError) as exc:
            if clock.timestamp_ns() >= deadline_ns:
                raise VenueAccountModeUnsupported(
                    f"could not read the abstraction mode of account {address} within "
                    f"{timeout_seconds}s ({exc}); refusing to start rather than assume "
                    f"it is {_accepted()}"
                ) from exc
            await clock.sleep(backoff_seconds)
            backoff_seconds = min(backoff_seconds * 2, _RETRY_MAX_BACKOFF_SECONDS)
            continue
        if mode in SUPPORTED_ACCOUNT_MODES:
            return
        raise VenueAccountModeUnsupported(_remediation(mode, address=address))


async def _read_account_mode(info: InfoRead, *, address: str) -> str:
    """The one unsigned ``userAbstraction`` read, as a mode literal.

    A body that is not a string is not a mode — it is the venue changing its
    contract — so it raises into the caller's retry rather than reaching the
    allowlist. The distinction is load-bearing in the message as much as in the
    control flow: the allowlist's refusal prints a remediation, and printing one
    here would send an operator to re-set a mode that was never the problem.

    Asked about the **trading** account rather than the signing key's own
    address: with an agent wallet those differ, and the mode is a property of
    the account whose numbers the ledger is bound to.
    """
    response = await info({"type": "userAbstraction", "user": address})
    if not isinstance(response, str):
        raise ValueError(f"unrecognized userAbstraction response: {response!r}")
    return response


def _accepted() -> str:
    return " or ".join(repr(mode) for mode in sorted(SUPPORTED_ACCOUNT_MODES))


def _remediation(mode: object, *, address: str) -> str:
    """The refusal, written as instructions rather than as a complaint.

    Roughly 88 % of sampled leaderboard-ranked accounts sit on a mode this
    refuses (ADR-0046 §1), so an operator meeting this line is the common case,
    and the fix is two **user-signed** actions — attributed to the signer, never
    to the ``user`` field in the payload, so an agent wallet provably cannot
    perform them however the call is addressed. The engine cannot do this for
    itself, which is the whole reason the error has to say what to do.
    """
    return (
        f"account {address} is in abstraction mode {mode!r}; Tickwright supports "
        f"Manual/Standard only ({_accepted()}). Under a pooled mode the perps "
        "clearinghouse reports only the collateral posted into perps, so account "
        "equity and free margin read an order of magnitude low with nothing in the "
        "response indicating it. To fix, with the master wallet (both steps are "
        'user-signed; an agent wallet cannot): (1) userSetAbstraction("disabled"), '
        "then (2) usdClassTransfer the account's USDC from spot to perps."
    )
