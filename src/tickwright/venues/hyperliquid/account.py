"""The account half of the Hyperliquid boundary — venue account facts, normalized.

The one place a Hyperliquid account identity becomes ``domain``, and the one
place ``clearinghouseState`` does: nothing else in the codebase composes a venue
account id or names a Hyperliquid field for these quantities, so a change to
what qualifies an account — or a seventh correction to what a field means — is a
one-file change (ADR-0031, ADR-0045 §3 — venue conventions are normalized in the
adapter, never in ``domain``).
"""

from decimal import Decimal

from tickwright.domain import AccountSpec, Netting, VenueAccountState

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
    """
    match response:
        case {
            "marginSummary": {"accountValue": str(equity)},
            "crossMarginSummary": {
                "accountValue": str(cross_equity),
                "totalMarginUsed": str(cross_margin_used),
            },
            "crossMaintenanceMarginUsed": str(cross_maintenance),
        }:
            return VenueAccountState(
                equity=Decimal(equity),
                free_margin=Decimal(cross_equity) - Decimal(cross_margin_used),
                cross_maintenance_margin=Decimal(cross_maintenance),
            )
    return None
