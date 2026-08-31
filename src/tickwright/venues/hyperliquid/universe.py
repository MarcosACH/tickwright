"""The venue's instrument universe (ADR-0030): specs plus asset indexing.

Perps are indexed by position in the meta ``universe`` array, and every order
action addresses the asset by that index — venue knowledge the adapter needs
alongside the venue-agnostic ``InstrumentSpec``s the guard consumes. One
value object carries both, so the composition root fetches once and wires
each half where it belongs (ADR-0031).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from tickwright.domain import InstrumentSpec

from . import transport
from .config import HyperliquidConfig
from .transport import PostJson

# Perp price-grid constants (ADR-0017/0030): prices carry at most 5 significant
# figures and MAX_DECIMALS − szDecimals decimal places, MAX_DECIMALS = 6 for
# perps (spot's 8 is a later, additive extension).
_PERP_MAX_DECIMALS = 6
_PERP_MAX_SIG_FIGS = 5
# The venue-wide minimum order value: $10 notional.
_MIN_NOTIONAL = Decimal("10")


@dataclass(frozen=True, slots=True, kw_only=True)
class HyperliquidUniverse:
    """The meta endpoint's perp universe: per-symbol specs and asset indices."""

    specs: Mapping[str, InstrumentSpec]
    asset_indices: Mapping[str, int]


async def fetch_instrument_specs(
    config: HyperliquidConfig, *, post: PostJson | None = None
) -> HyperliquidUniverse:
    """Source the ``InstrumentSpec`` universe from the venue meta endpoint.

    The venue is the authority on its own instruments (ADR-0031): each
    ``universe`` entry becomes one spec (``szDecimals`` from the venue, the
    perp price-grid constants, the $10 minimum), and its array position is the
    asset index order actions address. Read once by the composition root,
    which wires specs into the venue-agnostic guard and the whole universe
    into the exchange adapter.
    """
    # Late-bound default: resolving ``transport.post_json`` at call time keeps
    # the HTTP boundary patchable where injection has no path (the composition
    # root's arm), unlike a def-time-bound default argument.
    send = post if post is not None else transport.post_json
    response = await send(f"{config.api_url}/info", {"type": "meta"})
    match response:
        case {"universe": list(entries)}:
            pass
        case _:
            raise ValueError(f"unrecognized Hyperliquid meta response: {response!r}")
    specs: dict[str, InstrumentSpec] = {}
    asset_indices: dict[str, int] = {}
    for index, entry in enumerate(entries):
        name = str(entry["name"])
        # The venue's own leverage cap, and the flat tier-0 maintenance rate it
        # implies (ADR-0040 §4). Defaulted to ``1`` for an entry that publishes
        # no cap, matching ``InstrumentSpec``'s own default rather than inventing
        # a permissive one.
        max_leverage = int(entry.get("maxLeverage", 1))
        specs[name] = InstrumentSpec(
            symbol=name,
            sz_decimals=int(entry["szDecimals"]),
            max_decimals=_PERP_MAX_DECIMALS,
            min_notional=_MIN_NOTIONAL,
            max_sig_figs=_PERP_MAX_SIG_FIGS,
            max_leverage=max_leverage,
            margin_maint=_flat_maintenance_rate(max_leverage),
        )
        asset_indices[name] = index
    return HyperliquidUniverse(specs=specs, asset_indices=asset_indices)


def _flat_maintenance_rate(max_leverage: int) -> Decimal:
    """Hyperliquid's tier-0 maintenance fraction: half the initial margin at the
    asset's max leverage, i.e. ``1/(2·max_leverage)`` (ADR-0040 §4).

    Computed **here**, in the venue adapter, so the rule stays venue knowledge
    and ``domain``'s maintenance helper reads a plain ``notional ×
    margin_maint`` — the same split ADR-0036 made carrying the fee rates
    explicitly rather than re-deriving a fee tier.

    Flat tier-0 only: exact **below** an asset's first margin-tier band, and
    under-reporting above it, where ``meta.marginTables`` and the
    ``notional·mmr − deduction`` form take over. That extension point is
    deferred to ``InstrumentSpec.margin_table_id`` and deliberately not
    anticipated here.
    """
    return Decimal(1) / (2 * max_leverage)
