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
        specs[name] = InstrumentSpec(
            symbol=name,
            sz_decimals=int(entry["szDecimals"]),
            max_decimals=_PERP_MAX_DECIMALS,
            min_notional=_MIN_NOTIONAL,
            max_sig_figs=_PERP_MAX_SIG_FIGS,
        )
        asset_indices[name] = index
    return HyperliquidUniverse(specs=specs, asset_indices=asset_indices)
