"""Per-cloid continuous-absence trackers for the reconcile cycles.

The ``Reconciler`` never acts terminally on a *single* absent reading — that
would let an outage or a transient blip read as proof. Every cadence instead
requires **continuous** absence, and each ledger here concentrates the
reset-on-presence discipline: observe presence and the run is cleared; observe
absence and the run grows until it crosses a threshold.

**What is "absent" is the caller's to name.** These types know only that
something the caller was waiting for did not arrive, so they serve any question
of that shape. Two are live: a venue *record* absent from a read (the ghost
cycle and the in-flight retry budget), and a *readable body* absent from one
(ADR-0049's unreadable-body span, where the venue answered and the answer could
not be parsed). Keeping the vocabulary at "absent/present" rather than "no
record" is what lets the second question reuse the first's ledger instead of
minting a third.

The two types differ in the **unit** the run is measured in, and that is a
property of the question rather than of the caller:

* ``ConsecutiveMisses`` counts absent **observations** — the in-flight retry
  budget (ADR-0011 inv 7). Its unit is right precisely because the thing being
  budgeted *is* the polling: a missed poll is the unit of retry, so the count
  holds regardless of how the runner spaces its calls.
* ``GraceWindow`` measures absent **wall-clock time** — ADR-0011 inv 3's ghost
  grace window, and ADR-0049's unreadable-body span. Both ask whether waiting
  has been tried, and waiting is measured on a clock: a count would mean a
  different amount of it under each driver, and the unreadable span has three
  drivers polling as far apart as 5s, 30s and the startup barrier's backoff.

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
    once ``span_ns`` has elapsed since then with no intervening presence: the
    resting order is a ghost past its grace window (ADR-0011 inv 3), or the
    unreadable body has stayed unreadable long enough to be durable (ADR-0049).
    Any ``record_present`` in between clears the run, so only *continuous*
    absence, never a transient blip, ever fires. Firing clears the run so it
    re-arms.

    **The first observation only starts the clock**, whatever the span — so a
    single absent reading can never fire, and "more than one sample" is
    structural here rather than a threshold a caller has to configure for.
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
