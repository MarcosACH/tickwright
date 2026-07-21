"""Tickwright — an event-driven algorithmic trading engine.

A readable reference implementation: a market feed becomes orders through an
event-driven pipeline (``MarketFeed -> Strategy -> Exchange``) coordinated by an
``EventBus``, with a crash-safe order-lifecycle saga, idempotent recovery, and
exchange reconciliation.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("tickwright")
except PackageNotFoundError:  # not installed (e.g. running from a raw checkout)
    __version__ = "0.0.0+unknown"
