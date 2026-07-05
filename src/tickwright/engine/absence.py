"""Per-cloid consecutive-absence trackers for the continuous reconcile cycles.

The ``Reconciler`` never resolves an order terminally on a *single* absent read
— that would let an outage or a transient blip read as "gone". Both continuous
cadences instead require **continuous** absence before they act, and each owns a
small ledger that concentrates the reset-on-presence discipline: observe a
present record and the run is cleared; observe absence and the run grows until it
crosses the cadence's threshold.

The two cadences measure the run in genuinely different units, so they are two
types, not one parametrized ledger:

* ``ConsecutiveMisses`` counts absent **observations** — the in-flight retry
  budget (ADR-0011 inv 7). A missed poll is the unit of retry, so it holds
  regardless of how the runner spaces its calls.
* ``GraceWindow`` measures absent **wall-clock time** — the ghost grace window
  (ADR-0011 inv 3). A resting order must be absent across the whole span, so the
  unit must be the clock, not a call count.

Both are deliberately in-memory: a restart resets them, and the startup pass
re-proves every saga against venue truth before anything places (ADR-0009).
"""


class ConsecutiveMisses:
    """Counts consecutive absent observations per key; resets on presence.

    ``record_absent`` returns ``True`` once a key has been absent for ``limit``
    consecutive observations — the in-flight order is then proven never-landed
    (ADR-0008/0011). Firing clears the run, so a later absence re-arms it.
    """

    def __init__(self, *, limit: int) -> None:
        self._limit = limit
        self._misses: dict[str, int] = {}

    def record_present(self, key: str) -> None:
        """A record appeared for ``key``: the absence run resets to nothing."""
        self._misses.pop(key, None)

    def record_absent(self, key: str) -> bool:
        """Count one absent observation for ``key``; ``True`` once the budget is
        exhausted. The budget-exhausting miss is the proof, never the first —
        while the send may still be on the wire (ADR-0008 rule 2)."""
        misses = self._misses.get(key, 0) + 1
        if misses < self._limit:
            self._misses[key] = misses
            return False
        self._misses.pop(key, None)
        return True


class GraceWindow:
    """Measures continuous absent wall-clock time per key; resets on presence.

    ``record_absent`` stamps the first absent observation and returns ``True``
    once ``span_ns`` has elapsed since then with no intervening presence — the
    resting order is then a ghost past its grace window (ADR-0011 inv 3). Any
    ``record_present`` in between clears the run, so only *continuous* absence,
    never a transient blip, ever fires. Firing clears the run so it re-arms.
    """

    def __init__(self, *, span_ns: int) -> None:
        self._span_ns = span_ns
        self._first_absent_ns: dict[str, int] = {}

    def record_present(self, key: str) -> None:
        """A record is back (or never left) for ``key``: the grace clock resets."""
        self._first_absent_ns.pop(key, None)

    def record_absent(self, key: str, *, now_ns: int) -> bool:
        """Record absence for ``key`` at ``now_ns``; ``True`` once the span has
        elapsed since the first absent observation of this run."""
        first_absent_ns = self._first_absent_ns.setdefault(key, now_ns)
        if now_ns - first_absent_ns >= self._span_ns:
            self._first_absent_ns.pop(key)
            return True
        return False
