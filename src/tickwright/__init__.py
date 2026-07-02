"""Tickwright — an event-driven algorithmic trading engine.

A readable reference implementation: a market feed becomes orders through an
event-driven pipeline (``MarketFeed -> Strategy -> Exchange``) coordinated by an
``EventBus``, with a crash-safe order-lifecycle saga, idempotent recovery, and
exchange reconciliation.
"""

__version__ = "0.1.0"
