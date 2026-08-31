"""Leverage settings, venue-agnostic."""

from dataclasses import dataclass


@dataclass(frozen=True)
class LeverageSetting:
    """One symbol's leverage and margin mode."""

    leverage: int
    mode: str = "isolated"


@dataclass(frozen=True)
class InstrumentSpec:
    """The venue-sourced half of the leverage bound."""

    symbol: str
    max_leverage: int = 1
    margin_maint: float = 0.0


def check_leverage(setting: LeverageSetting, spec: InstrumentSpec) -> None:
    """Raise if `setting` is outside `1 <= leverage <= spec.max_leverage`."""
    if setting.leverage < 1:
        raise ValueError(f"{spec.symbol}: leverage below 1")
