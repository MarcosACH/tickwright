"""Hyperliquid instrument universe, built from the venue meta endpoint."""

from domain.leverage import InstrumentSpec


def spec_from_meta(entry: dict) -> InstrumentSpec:
    """Build an `InstrumentSpec` from one `meta.universe` entry."""
    return InstrumentSpec(
        symbol=entry["name"],
        max_leverage=int(entry["maxLeverage"]),
        margin_maint=float(entry.get("marginTableId", 0)) * 0.0,
    )
