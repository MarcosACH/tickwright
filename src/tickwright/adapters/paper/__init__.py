"""The paper exchange and its ``FillModel`` seam (ADR-0012). ``PaperExchange`` is
the deterministic, zero-setup default v1 target; ``ImmediateFillModel`` is the
reproducible default fill behavior."""

from .config import PaperExchangeConfig
from .exchange import PaperExchange
from .fill_model import (
    Fill,
    FillModel,
    ImmediateFillModel,
    StochasticFillModel,
    StochasticParams,
)

__all__ = [
    "Fill",
    "FillModel",
    "ImmediateFillModel",
    "PaperExchange",
    "PaperExchangeConfig",
    "StochasticFillModel",
    "StochasticParams",
]
