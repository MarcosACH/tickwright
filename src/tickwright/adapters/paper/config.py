"""``PaperExchange`` config — each adapter's config lives in its package
(ADR-0032); only the composition root reads them all."""

from pydantic import BaseModel, Field

from tickwright.domain import InstrumentSpec


class PaperExchangeConfig(BaseModel):
    """Config-sourced venue metadata (ADR-0031): the paper venue has no meta
    endpoint, so its per-symbol ``InstrumentSpec``s come from here. The
    composition root also wires them into the venue-agnostic guard."""

    instrument_specs: dict[str, InstrumentSpec] = Field(default_factory=dict)
