"""Store adapters. ``SQLiteStore`` is the zero-setup default (ADR-0019); the
``PostgresStore`` production backend lands in a later slice."""

from .sqlite import SQLiteStore

__all__ = ["SQLiteStore"]
