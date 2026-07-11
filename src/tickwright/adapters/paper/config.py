"""``PaperExchange`` config — each adapter's config lives in its package
(ADR-0032); only the composition root reads them all."""

from typing import Literal

from pydantic import BaseModel, Field

from tickwright.domain import InstrumentSpec

from .fill_model import StochasticParams


class PaperExchangeConfig(BaseModel):
    """Config-sourced venue metadata (ADR-0031): the paper venue has no meta
    endpoint, so its per-symbol ``InstrumentSpec``s come from here. The
    composition root also wires them into the venue-agnostic guard.

    The ``fill_model`` discriminant is the one nondeterminism knob (ADR-0012):
    ``immediate`` (the reproducible default) needs neither ``seed`` nor the
    ``stochastic`` knobs; ``stochastic`` reads the ``seed`` (to build the RNG)
    plus the ``StochasticParams`` bundle (nested under ``stochastic``), whose
    defaults are inert so an unseeded stochastic model still behaves
    optimistically.
    """

    instrument_specs: dict[str, InstrumentSpec] = Field(default_factory=dict)
    fill_model: Literal["immediate", "stochastic"] = "immediate"
    seed: int = 0
    stochastic: StochasticParams = StochasticParams()
