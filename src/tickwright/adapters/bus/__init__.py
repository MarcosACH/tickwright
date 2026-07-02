"""EventBus adapters. ``InMemoryBus`` is the hermetic default (ADR-0023); the
``KafkaBus`` durable backend lands in a later slice."""

from .inmemory import InMemoryBus

__all__ = ["InMemoryBus"]
