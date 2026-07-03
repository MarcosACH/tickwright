"""MarketFeed adapters. ``ReplayFeed`` is the deterministic, file-backed feed
(ADR-0027); the live ``HyperliquidFeed`` lives in its venue package."""

from .replay import ReplayFeed

__all__ = ["ReplayFeed"]
