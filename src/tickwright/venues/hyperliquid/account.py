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
from tickwright.observability import NamedEvent, named_event

from .config import HyperliquidConfig


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


def normalize_account_state(response: object) -> VenueAccountState | None:
    """A ``clearinghouseState`` body as ``domain``, or ``None`` if it is not one.

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

    A body this cannot read is a **failed read**, answered ``None`` and named:
    every branch out of here is either a whole state or no state at all, so a
    venue contract change freezes the reconcile rather than degrading into a
    partial account that would read as truth (ADR-0011 inv 1).
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
            try:
                return VenueAccountState(
                    equity=Decimal(equity),
                    free_margin=Decimal(cross_equity) - Decimal(cross_margin_used),
                    cross_maintenance_margin=Decimal(cross_maintenance),
                    positions=tuple(_position(entry["position"]) for entry in entries),
                )
            except (ArithmeticError, KeyError, TypeError) as exc:
                # The root shape matched but something inside it did not: a
                # position row missing a field, a figure that is not a number.
                _name_failed_read(f"{exc!r} in clearinghouseState response {response!r}")
                return None
    _name_failed_read(f"unrecognized clearinghouseState response: {response!r}")
    return None


def _name_failed_read(error: str) -> None:
    """Name a read that came back unreadable rather than unreachable.

    The named event is the difference between the two ways this read comes back
    empty: a transport failure is a connection, and this is the *response* — the
    shape a venue contract change would arrive as. It must stay visible rather
    than look like a quiet outage.
    """
    named_event(NamedEvent.EXCHANGE_REQUEST_FAILED, request="clearinghouseState", error=error)


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
    margin_used = Decimal(reported["marginUsed"])
    unrealized_pnl = Decimal(reported["unrealizedPnl"])
    entry_price = reported.get("entryPx")
    liquidation_price = reported["liquidationPx"]
    return VenuePositionState(
        symbol=str(reported["coin"]),
        signed_size=Decimal(reported["szi"]),
        entry_price=None if entry_price is None else Decimal(entry_price),
        notional=Decimal(reported["positionValue"]),
        unrealized_pnl=unrealized_pnl,
        margin_used=margin_used,
        isolated_collateral=_isolated_collateral(
            reported["leverage"], margin_used=margin_used, unrealized_pnl=unrealized_pnl
        ),
        liquidation_price=None if liquidation_price is None else Decimal(liquidation_price),
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
    """
    if leverage["type"] != "isolated":
        return None
    return margin_used - unrealized_pnl
