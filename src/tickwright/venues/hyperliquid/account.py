"""The account half of the Hyperliquid boundary — venue account facts, normalized.

The one place a Hyperliquid account identity becomes ``domain``, and the one
place ``clearinghouseState`` does: nothing else in the codebase composes a venue
account id or names a Hyperliquid field for these quantities, so a change to
what qualifies an account — or a seventh correction to what a field means — is a
one-file change (ADR-0031, ADR-0045 §3 — venue conventions are normalized in the
adapter, never in ``domain``).
"""

from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Final, assert_never, cast, get_args

from tickwright.domain import (
    AccountSpec,
    LeverageSpec,
    MarginMode,
    Netting,
    VenueAccountState,
    VenuePositionState,
)

from .config import HyperliquidConfig
from .reading import figure

_REPORTED_MARGIN_MODES: Final = frozenset(get_args(MarginMode.__value__))
"""The two ``leverage.type`` literals, taken from ``domain``'s own alias.

Derived rather than transcribed: config and the venue read must agree on what a
margin mode *is*, and a hand-copied pair here would be free to fall behind the
type the comparison is made against. Through ``__value__`` because ``MarginMode``
is a PEP 695 ``type`` statement — a ``TypeAliasType`` whose ``get_args`` is
empty, which would make this an allowlist that refuses **every** mode."""


def account_spec(config: HyperliquidConfig, *, address: str) -> AccountSpec:
    """The venue's static declaration about the account this process trades.

    The id is qualified **venue + network + venue-native identifier** — three
    segments, against the paper venue's two (ADR-0038/0042 §5) — and ``address``
    is the *trading* account, which is the signing key's own address only when
    the key is not an API/agent wallet acting for a master account.

    ``genesis_collateral`` is ``None`` on live and that absence is the decision,
    not an omission: the account's opening state is read from the venue rather
    than configured (ADR-0042 §6), and the ``None`` is what the startup checks
    read to tell the two paths apart.
    """
    network = "testnet" if config.testnet else "mainnet"
    return AccountSpec(
        account_id=f"hyperliquid-{network}-{address}",
        netting=Netting.NET,
        genesis_collateral=None,
    )


def held_leverage(response: object) -> dict[str, LeverageSpec]:
    """The same body's *stored* margin setting per held symbol, as config's type.

    A second read of ``clearinghouseState`` rather than a field on
    ``VenuePositionState``, and both halves of that are deliberate. It lives
    **here** because this module is the one place a Hyperliquid field name for
    these quantities appears, and the boot-time leverage push needs
    ``assetPositions`` like every other account-grain read does. It is **not** a
    position-state field because ``VenuePositionState`` models what a position
    *is worth*, and the push consumes the setting at boot, before any
    projection exists to carry it; extending the state object is the post-boot
    drift check's problem, not this read's.

    Returned as ``LeverageSpec`` rather than a pair, so the push's comparison is
    one equality against the value an operator wrote and cannot drift by
    comparing halves. Symbols the account is flat in are simply **absent** — the
    venue reports the setting for exactly the positions it holds, which is what
    makes one read enough for a whole boot (ADR-0044 §4).

    The mode is checked against the two literals the venue reports rather than
    trusted into ``LeverageSpec``, which is a plain frozen dataclass and would
    take a third one silently. Silently is the problem: an unrecognised mode
    compares unequal to every configured spec, so the boot would read a venue
    contract change as a *disagreement about a held position* — the branch that
    refuses to start, sending an operator to fix a leverage that was never
    wrong. Raised as the ``ValueError`` ``reading.UNREADABLE`` already names for
    exactly this ("a margin mode outside the two the venue reports").
    """
    if not isinstance(response, Mapping):
        raise TypeError(f"non-mapping clearinghouseState response {response!r}")
    held: dict[str, LeverageSpec] = {}
    for row in response["assetPositions"]:
        position = row["position"]
        setting = position["leverage"]
        held[position["coin"]] = LeverageSpec(
            mode=reported_margin_mode(setting), leverage=setting["value"]
        )
    return held


def reported_margin_mode(setting: Mapping[str, Any]) -> MarginMode:
    """One ``leverage`` sub-object's ``type``, as ``domain``'s own literal.

    The single gate on what this venue is allowed to call a margin mode, read by
    both consumers of the field in this module. Two checks would be two answers:
    the allowlist here is *derived* from ``MarginMode`` while a hand-written
    ``match`` is not, so a third literal added to the alias would widen one and
    not the other, and the two functions reading one field would disagree about
    whether the body was legible.

    Refusing is the only branch that cannot be wrong, and it is the same refusal
    for both callers. ``held_leverage`` must not trust an unrecognised mode into
    ``LeverageSpec`` — a plain frozen dataclass would take it silently, and it
    would then compare unequal to every configured spec, so the boot would read
    a venue contract change as a *held disagreement* and send an operator to fix
    a leverage that was never wrong. ``_isolated_collateral`` must not answer one
    as cross — its ``None`` is the positive claim *this position is backed by the
    account pool*, and nothing downstream can tell that apart from a measured
    cross read.

    Raised as the ``ValueError`` ``reading.UNREADABLE`` already names for exactly
    this case ("a margin mode outside the two the venue reports").
    """
    mode = setting["type"]
    if mode not in _REPORTED_MARGIN_MODES:
        raise ValueError(f"unrecognized margin mode {mode!r} in clearinghouseState")
    return cast(MarginMode, mode)


def normalize_account_state(response: object) -> VenueAccountState:
    """A ``clearinghouseState`` body as ``domain``.

    Three account-grain figures, each from the field two rounds of testnet and
    mainnet measurement pinned (ADR-0046 §2, §2.1):

    - **equity** is ``marginSummary.accountValue`` — the whole account marked to
      market, isolated positions included.
    - **free margin** is ``crossMarginSummary.accountValue −
      crossMarginSummary.totalMarginUsed``. The root ``withdrawable`` figure is
      **not read at all**: it additionally deducts the initial margin reserved by
      resting orders (measured exactly: a `25.68` gap on one 128.40-notional
      order at 5x) plus a 10 %-of-notional withdrawal haircut, and it answers
      *"how much could I take off the venue"* rather than *"how much free
      collateral does this account have"*. Since ADR-0024 leaves resting ``LIVE``
      orders on the venue across a graceful stop, that gap is the normal state
      and is unbounded, so no tolerance could absorb it.
    - **cross maintenance margin** is the root ``crossMaintenanceMarginUsed``,
      which is **cross-only** — the asymmetry is inside one response, since
      ``marginSummary.totalMarginUsed`` *includes* isolated positions and nothing
      but the ``cross`` prefix says which is which.

    Plus one row per open position — a coin the account is flat in is simply
    absent from ``assetPositions``, the venue running one-way net positions.

    A body this cannot read **raises** into ``UNREADABLE``, and ``read`` turns
    that into the named ``VenueReadFailure`` the reconcile freezes on (collapsed
    to ``None`` by ``fetch_account_state``, this grain's caller): every branch
    out of here is either a whole state or no state at all, so a contract change
    freezes rather than degrading into a partial account that would read as
    truth (ADR-0011 inv 1). Deciding what that refusal *means* is deliberately
    not this function's job — it reads a body, and one module owns the verdict
    for every grain of this venue (ADR-0048).

    The figures inside can fail after the root shape matched: a position row
    missing a field, a figure that is not a number — or is one the venue
    re-typed or reported non-finite, both of which ``figure`` refuses — a margin
    mode outside the two the venue reports (neither ``figure`` nor
    ``_isolated_collateral`` guesses what it means). What refusing buys *this*
    grain: a reconcile handed a ``NaN`` equity never agrees with the ledger
    (``nan == nan`` is false), so it would read permanent divergence and heal
    toward a figure no venue holds, and any tolerance comparison it made would
    signal an ``InvalidOperation`` mid-cycle rather than freezing on the read
    that admitted it. An infinity drives that same heal unbounded.
    """
    match response:
        case {
            "marginSummary": {"accountValue": str(equity)},
            "crossMarginSummary": {
                "accountValue": str(cross_equity),
                "totalMarginUsed": str(cross_margin_used),
            },
            "crossMaintenanceMarginUsed": str(cross_maintenance),
            "assetPositions": list(entries),
        }:
            return VenueAccountState(
                equity=figure(equity),
                free_margin=figure(cross_equity) - figure(cross_margin_used),
                cross_maintenance_margin=figure(cross_maintenance),
                positions=tuple(_position(entry["position"]) for entry in entries),
            )
    raise ValueError("unrecognized clearinghouseState response")


def _position(reported: Mapping[str, Any]) -> VenuePositionState:
    """One ``assetPositions[].position`` row as ``domain``.

    ``liquidationPx`` rides through verbatim, ``null`` included: the venue omits
    it whenever the price would be non-positive, which is the majority case for a
    long (ADR-0046 §6), and nothing here may substitute a number for it.

    ``entryPx`` is the one field here the venue types **optional**, so its absence
    is a legal response and must not freeze the reconcile — the freeze is for
    responses we cannot read, and no comparison depends on this field. Every
    other field is required: missing one means we are not reading what we think
    we are, and the caller turns that into a failed read.
    """
    margin_used = figure(reported["marginUsed"])
    unrealized_pnl = figure(reported["unrealizedPnl"])
    entry_price = reported.get("entryPx")
    liquidation_price = reported["liquidationPx"]
    return VenuePositionState(
        symbol=str(reported["coin"]),
        signed_size=figure(reported["szi"]),
        entry_price=None if entry_price is None else figure(entry_price),
        notional=figure(reported["positionValue"]),
        unrealized_pnl=unrealized_pnl,
        margin_used=margin_used,
        isolated_collateral=_isolated_collateral(
            reported["leverage"], margin_used=margin_used, unrealized_pnl=unrealized_pnl
        ),
        liquidation_price=None if liquidation_price is None else figure(liquidation_price),
    )


def _isolated_collateral(
    leverage: Mapping[str, Any], *, margin_used: Decimal, unrealized_pnl: Decimal
) -> Decimal | None:
    """The position's own locked bucket, or ``None`` when it is cross-margined.

    Recovered as ``marginUsed − unrealizedPnl``, **never** from
    ``leverage.rawUsd``. That field is the position's cash leg net of its cost
    basis, so it measures *negative* for a long — `−103.731933` against a
    measured collateral of `25.898067` on the same position — and reading it
    would report a funded long as owing the venue money. The subtraction is the
    venue's own identity read backwards: its ``marginUsed`` for an isolated
    position *is* ``collateral + unrealizedPnl``, and moves with the mark while
    ``rawUsd`` holds still.

    The two modes are matched **explicitly** and anything else is a failed read,
    because ``None`` here is not "unknown" — it is the positive claim *this
    position is backed by the account pool*, and downstream nothing can tell it
    apart from a measured cross read. A third or renamed mode answered as cross
    would report a position holding a locked bucket as pool-backed, inflating
    free margin by the whole bucket; freezing instead is inv 1 at the one grain
    where this module could otherwise degrade quietly (ADR-0044 pins the pair
    ``cross``/``isolated``).

    The refusal is ``reported_margin_mode``'s rather than one written here, which
    makes the match below exhaustive over ``MarginMode`` and the ``assert_never``
    a **type error** rather than a runtime one: a third literal added to the
    alias stops this file compiling, at the one branch that would otherwise have
    to guess what it means.
    """
    mode = reported_margin_mode(leverage)
    match mode:
        case "cross":
            return None
        case "isolated":
            return margin_used - unrealized_pnl
    # Reached only if ``MarginMode`` grows a literal this match does not answer,
    # and then not at runtime: ``mode`` is narrowed to the unhandled literal
    # here, so the call is a *type* error. Bound to a name rather than written
    # ``assert_never(leverage["type"])`` — that expression is ``Any``, which
    # ``assert_never`` accepts unconditionally, and the guard would silently
    # check nothing (verified by adding a third literal and watching mypy pass).
    assert_never(mode)
