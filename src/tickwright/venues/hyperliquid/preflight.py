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

It is the first of the two guards, and ADR-0044 §7's **leverage push** is the
second: it runs behind the gate and is gated by it, because the push's own
three-way split is computed against a margin model a pooled account invalidates.
Isolated from the adapter because both are refusals that must fire before the
barrier and before any order, and both fail **closed** — which is testable
against recorded responses only while it is reachable without going near the
order path. It also keeps the one *signed venue write* in the whole accounting
surface in a module a reader can audit in full.
"""

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from tickwright.domain import Clock, LeverageBook, VenueAccountModeUnsupported

from .backoff import Backoff
from .reading import UNREADABLE

InfoRead = Callable[[dict[str, Any]], Awaitable[object]]
"""One unsigned ``POST /info`` query, as the adapter makes it."""

SignedSend = Callable[[dict[str, Any]], Awaitable[object]]
"""One signed ``POST /exchange`` action, as the adapter sends an order.

The same path the order verbs take, rather than a second signing stack: the
nonce floor, the mainnet/testnet domain and the resolved trading account are all
decisions the adapter already makes once, and a write that re-made any of them
would be free to disagree with the orders it boots alongside."""

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

The *values* are matched; the doubling itself is the package's own ``Backoff``,
the one home this venue keeps that rule in so its retry loops cannot skew. What
this read cannot share is the barrier's copy: it has to clear before the
barrier's own venue reads run, and ``venues`` may not import ``engine``
(ADR-0032). So the deadline arithmetic below is written twice — here and in
``engine/barrier.py`` — under the same budget the barrier is given (ADR-0044 §6:
one boot-time budget, never a second timeout).

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
    backoff = Backoff(initial=_RETRY_INITIAL_BACKOFF_SECONDS, maximum=_RETRY_MAX_BACKOFF_SECONDS)
    while True:
        try:
            mode = await _read_account_mode(info, address=address)
        # The two ways a boot read comes back empty, answered identically
        # because at boot they cost the same thing: the venue was unreachable
        # (``OSError``), or it answered with a body that is not a mode
        # (``UNREADABLE`` — the venue read vocabulary every other grain of this
        # adapter already catches, rather than a fourth hand-picked tuple).
        except (OSError, *UNREADABLE) as exc:
            if clock.timestamp_ns() >= deadline_ns:
                raise VenueAccountModeUnsupported(
                    f"could not read the abstraction mode of account {address} within "
                    f"{timeout_seconds}s ({exc}); refusing to start rather than assume "
                    f"it is {_accepted()}"
                ) from exc
            await backoff.sleep_on(clock)
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

    Raised as the ``TypeError`` ``reading.UNREADABLE`` already names, and for
    the reason ``figure`` refuses a re-typed number: a body of the wrong type is
    the same "we are not reading what we think we are" a missing field is. This
    grain has no figure to parse, but it has the same contract to lose.

    Asked about the **trading** account rather than the signing key's own
    address: with an agent wallet those differ, and the mode is a property of
    the account whose numbers the ledger is bound to.
    """
    response = await info({"type": "userAbstraction", "user": address})
    if not isinstance(response, str):
        raise TypeError(f"non-string userAbstraction response {response!r}")
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


async def push_leverage(
    *,
    info: InfoRead,
    send: SignedSend,
    address: str,
    book: LeverageBook,
    asset_indices: Mapping[str, int],
) -> None:
    """Align the venue to config, once, at boot (ADR-0044 §7).

    Config wins at startup and the venue wins in flight: this runs at ADR-0024
    step 4, behind the mode gate and ahead of the barrier, and never again. An
    operator who lowers a leverage in the venue UI to de-risk a live position
    must not be silently reverted by a later re-push.

    Scope is every symbol in the resolved ``book`` — the strategy-traded set,
    the defaulted symbols included, and neither the perp universe nor the feed
    list (§3). Skipping a symbol nobody configured would leave the venue holding
    whatever leverage it had while the model computes full-notional margin for
    it, *understating* the position's risk in the one direction that matters.

    One unsigned ``clearinghouseState`` read serves the whole boot rather than
    one ``activeAssetData`` read per symbol (§4): the risky symbols are exactly
    the ones holding a position, and that read reports leverage for exactly
    those.
    """
    if not book.entries:
        # A run with nothing traded is a legitimate book, not an unresolved one
        # — so there is no symbol to align and no reason to ask the venue about
        # an account whose answer nothing would read.
        return
    await info({"type": "clearinghouseState", "user": address})
    for symbol in sorted(book.entries):
        spec = book.for_symbol(symbol)
        await send(
            {
                "type": "updateLeverage",
                "asset": asset_indices[symbol],
                "isCross": spec.mode == "cross",
                "leverage": spec.leverage,
            }
        )
