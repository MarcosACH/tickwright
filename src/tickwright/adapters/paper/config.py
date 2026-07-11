"""``PaperExchange`` config — each adapter's config lives in its package
(ADR-0032); only the composition root reads them all."""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from tickwright.domain import InstrumentSpec


class PaperExchangeConfig(BaseModel):
    """Config-sourced venue metadata (ADR-0031): the paper venue has no meta
    endpoint, so its per-symbol ``InstrumentSpec``s come from here. The
    composition root also wires them into the venue-agnostic guard.

    The ``fill_model`` discriminant is the one nondeterminism knob (ADR-0012):
    ``immediate`` (the reproducible default) needs none of the seeded fields;
    ``stochastic`` reads the ``seed`` plus the slippage / queue / partial /
    latency knobs, whose defaults are inert so an unseeded stochastic model
    still behaves optimistically.
    """

    instrument_specs: dict[str, InstrumentSpec] = Field(default_factory=dict)
    fill_model: Literal["immediate", "stochastic"] = "immediate"
    seed: int = 0
    prob_slippage: float = 0.0
    max_slippage: Decimal = Decimal("0")
    prob_fill_on_limit: float = 1.0
    partial_fill_fraction: Decimal = Decimal("1")
    latency_seconds: float = 0.0
