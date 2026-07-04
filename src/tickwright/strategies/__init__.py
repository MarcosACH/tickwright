"""Minimal reference ``Strategy`` implementations (ADR-0016). These are an engine
capability, not a library; each shipped impl only proves the seam. This package
stays domain-only — a strategy emits no named events."""

from .single_shot import SingleShotMarketStrategy
from .single_shot_limit import SingleShotLimitStrategy

__all__ = ["SingleShotLimitStrategy", "SingleShotMarketStrategy"]
