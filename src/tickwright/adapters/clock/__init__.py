"""Clock adapters (ADR-0005): ``LiveClock`` (wall clock) and ``ManualClock``
(virtual time). This package stays domain-only — it emits no named events."""

from .live import LiveClock
from .manual import ManualClock

__all__ = ["LiveClock", "ManualClock"]
