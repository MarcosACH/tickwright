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
from typing import Any

from tickwright.domain import (
    AccountSpec,
    Netting,
    VenueAccountState,
    VenuePositionState,
)

from .config import HyperliquidConfig
from .reading import figure


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
    that into the named ``None`` the reconcile freezes on: every branch out of
    here is either a whole state or no state at all, so a venue contract change
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
    """
    match leverage["type"]:
        case "cross":
            return None
        case "isolated":
            return margin_used - unrealized_pnl
    raise ValueError(f"unrecognized margin mode {leverage['type']!r}")
