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

from tickwright.domain import VenueAccountModeUnsupported

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


async def verify_account_mode(*, info: InfoRead, address: str) -> None:
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
    """
    mode = await info({"type": "userAbstraction", "user": address})
    if mode in SUPPORTED_ACCOUNT_MODES:
        return
    raise VenueAccountModeUnsupported(_remediation(mode, address=address))


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
