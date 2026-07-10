"""The venue's instrument universe (ADR-0030): specs plus asset indexing.

Perps are indexed by position in the meta ``universe`` array, and every order
action addresses the asset by that index — venue knowledge the adapter needs
alongside the venue-agnostic ``InstrumentSpec``s the guard consumes. One
value object carries both, so the composition root fetches once and wires
each half where it belongs (ADR-0031).
"""

from collections.abc import Mapping
from dataclasses import dataclass

from tickwright.domain import InstrumentSpec


@dataclass(frozen=True, slots=True, kw_only=True)
class HyperliquidUniverse:
    """The meta endpoint's perp universe: per-symbol specs and asset indices."""

    specs: Mapping[str, InstrumentSpec]
    asset_indices: Mapping[str, int]
