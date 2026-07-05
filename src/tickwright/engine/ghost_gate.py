"""``GhostGate`` — the timing gate that decides when an absent order is a ghost.

ADR-0011 invariant 3 has two clauses, and this is both of them in one place: a
**recent-order protection window** in front of the **ghost grace window**. Given
an absent, non-terminal resting order, the gate returns a single verdict:

* ``PROTECTED`` — the order's last saga event is fresher than the protection
  window, so the venue's open-orders snapshot may simply not have propagated the
  ack yet. Skip ghost evaluation entirely; the grace clock is left un-armed so
  the order is never raced onto the ghost path.
* ``WAITING`` — past protection and absent, but not yet *continuously* absent
  across the grace window.
* ``GHOST`` — continuously absent across the grace window: resolve it terminally.

The gate composes a :class:`~tickwright.engine.absence.GraceWindow` for the
continuous-absence measurement and adds the recency pre-filter in front — so the
whole "is it eligible to be ghosted yet?" rule reads top-to-bottom here, rather
than smeared across the ``Reconciler``. It is a pure decision object: no bus, no
clock, no telemetry. The caller feeds it the current time and the order's last
recency (both nanoseconds) and acts on the verdict.
"""

from enum import Enum

from .absence import GraceWindow


class GhostVerdict(Enum):
    """The gate's ruling on one absent, non-terminal resting order."""

    PROTECTED = "protected"
    WAITING = "waiting"
    GHOST = "ghost"


class GhostGate:
    """The per-cloid ADR-0011 inv 3 gate: protection pre-filter, then grace window."""

    def __init__(self, *, grace_span_ns: int, protection_span_ns: int) -> None:
        self._grace = GraceWindow(span_ns=grace_span_ns)
        self._protection_span_ns = protection_span_ns

    def record_present(self, cloid: str) -> None:
        """A venue record is back (or the order healed terminal) for ``cloid``:
        the grace clock resets, since only *continuous* absence may ghost."""
        self._grace.record_present(cloid)

    def evaluate(self, cloid: str, *, now_ns: int, last_event_ns: int | None) -> GhostVerdict:
        """Rule on ``cloid`` given its absent reading at ``now_ns``.

        ``last_event_ns`` is the ``ts_ns`` of the order's most recent saga event
        this session, or ``None`` when its recency is unknown — a saga recovered
        from the store that the startup barrier already re-proved, so it is *not*
        protected and the grace window is its only guard. While the last event is
        fresher than the protection window the order is ``PROTECTED`` and the
        grace clock is *reset* — protection defers the start of the grace
        measurement, so a fresh event arriving after the clock had already armed
        (e.g. a ``cancel_requested`` marker on a still-resting order) restarts it
        rather than riding a stale run. Otherwise the grace clock advances and
        rules ``GHOST`` once the order has been continuously absent across it.
        """
        if last_event_ns is not None and now_ns - last_event_ns < self._protection_span_ns:
            self._grace.record_present(cloid)
            return GhostVerdict.PROTECTED
        if self._grace.record_absent(cloid, now_ns=now_ns):
            return GhostVerdict.GHOST
        return GhostVerdict.WAITING
