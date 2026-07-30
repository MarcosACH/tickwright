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
                    equity=_figure(equity),
                    free_margin=_figure(cross_equity) - _figure(cross_margin_used),
                    cross_maintenance_margin=_figure(cross_maintenance),
                    positions=tuple(_position(entry["position"]) for entry in entries),
                )
            except (ArithmeticError, KeyError, TypeError, ValueError) as exc:
                # The root shape matched but something inside it did not: a
                # position row missing a field, a figure that is not a number (or
                # is a *non-finite* one — see ``_figure``), a margin mode outside
                # the two the venue reports (the ``ValueError`` — neither
                # ``_figure`` nor ``_isolated_collateral`` guesses what it means).
                _name_failed_read(f"{exc!r} in clearinghouseState response {_rendered(response)}")
                return None
    _name_failed_read(f"unrecognized clearinghouseState response: {_rendered(response)}")
    return None


def _name_failed_read(error: str) -> None:
    """Name a read that came back unreadable rather than unreachable.

    The named event is the difference between the two ways this read comes back
    empty: a transport failure is a connection, and this is the *response* — the
    shape a venue contract change would arrive as. It must stay visible rather
    than look like a quiet outage.
    """
    named_event(NamedEvent.EXCHANGE_REQUEST_FAILED, request="clearinghouseState", error=error)


_RENDER_LIMIT = 300
"""Characters of response body a failed read carries. Enough for the value-shaped
failures; short of a fifty-position body."""


def _rendered(response: object) -> str:
    """``response`` bounded for a log line — its shape first, its body truncated.

    This branch is what a venue contract change arrives as, so it repeats on
    every reconcile cycle for as long as the contract stays broken: a
    fifty-position body would be kilobytes an operator does not need served fifty
    times over. The **key set** is what identifies a contract change, so it leads
    and is never truncated; the body follows, bounded, for the failures a key set
    cannot show — a figure that is not a number.

    Nothing in here may raise: it runs only on the path that has already decided
    to answer ``None``, and an exception escaping would turn a fail-closed read
    into a crashed one. Hence ``list`` over ``sorted`` on the keys — mixed key
    types have no order but always have a repr.
    """
    shape = f"keys={list(response)} " if isinstance(response, Mapping) else ""
    body = repr(response)
    if len(body) > _RENDER_LIMIT:
        body = f"{body[:_RENDER_LIMIT]}… ({len(body)} chars)"
    return f"{shape}{body}"


def _figure(reported: Any) -> Decimal:
    """One venue figure as an exact number, or a failed read if it is not one.

    Every quantity in this response goes through here, because a non-finite
    figure is the one unreadable value that does not announce itself:
    ``Decimal("nan")`` and ``Decimal("Infinity")`` are *valid* constructions, so
    they raise nothing and would ride through into a whole ``VenueAccountState``
    while a missing field or a non-numeric string freezes the read.

    Letting one through would be fail-*open* in the worst direction available at
    this grain. Every comparison against a ``NaN`` is false, so a reconcile
    handed a ``NaN`` equity would find no divergence to act on, read the ledger
    as agreeing with the venue and decline to freeze — the exact inversion of
    inv 1, and quieter than the outage it is supposed to behave like. An infinity
    is no better: it would drive an unbounded heal toward a figure no venue
    holds. So a figure that is not finite is a failed read like any other
    (ADR-0011 inv 1, ADR-0034 — never a fabricated number).
    """
    figure = Decimal(reported)
    if not figure.is_finite():
        raise ValueError(f"non-finite figure {reported!r}")
    return figure


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
    margin_used = _figure(reported["marginUsed"])
    unrealized_pnl = _figure(reported["unrealizedPnl"])
    entry_price = reported.get("entryPx")
    liquidation_price = reported["liquidationPx"]
    return VenuePositionState(
        symbol=str(reported["coin"]),
        signed_size=_figure(reported["szi"]),
        entry_price=None if entry_price is None else _figure(entry_price),
        notional=_figure(reported["positionValue"]),
        unrealized_pnl=unrealized_pnl,
        margin_used=margin_used,
        isolated_collateral=_isolated_collateral(
            reported["leverage"], margin_used=margin_used, unrealized_pnl=unrealized_pnl
        ),
        liquidation_price=None if liquidation_price is None else _figure(liquidation_price),
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
