"""``ConsecutiveMisses`` / ``GraceWindow`` — the reconcile absence trackers.

Each cadence resolves an order terminally only after *continuous* absence, and
these ledgers own that discipline. The two measure the run in different units —
missed observations vs elapsed wall-clock time — so they are tested as the two
distinct types they are.
"""

from tickwright.engine.absence import ConsecutiveMisses, GraceWindow


class TestConsecutiveMisses:
    def test_fires_only_on_the_limit_th_consecutive_miss(self) -> None:
        misses = ConsecutiveMisses(limit=3)
        # The first misses prove nothing while the send may still land.
        assert misses.record_absent("0xabc") is False
        assert misses.record_absent("0xabc") is False
        # The budget-exhausting miss is the proof.
        assert misses.record_absent("0xabc") is True

    def test_a_present_reading_resets_the_run(self) -> None:
        misses = ConsecutiveMisses(limit=3)
        misses.record_absent("0xabc")
        misses.record_absent("0xabc")
        misses.record_present("0xabc")
        # Reset means the count starts over — no carry-over toward a false FAILED.
        assert misses.record_absent("0xabc") is False
        assert misses.record_absent("0xabc") is False
        assert misses.record_absent("0xabc") is True

    def test_firing_re_arms_the_run(self) -> None:
        misses = ConsecutiveMisses(limit=2)
        assert misses.record_absent("0xabc") is False
        assert misses.record_absent("0xabc") is True
        # Firing cleared the run, so the next absence starts a fresh budget.
        assert misses.record_absent("0xabc") is False
        assert misses.record_absent("0xabc") is True

    def test_a_limit_of_one_fires_on_the_first_miss(self) -> None:
        misses = ConsecutiveMisses(limit=1)
        assert misses.record_absent("0xabc") is True

    def test_runs_are_independent_per_key(self) -> None:
        misses = ConsecutiveMisses(limit=2)
        assert misses.record_absent("0xabc") is False
        # A different cloid's miss must not advance this one toward its budget.
        assert misses.record_absent("0xdef") is False
        assert misses.record_absent("0xabc") is True


class TestGraceWindow:
    def test_fires_once_the_span_has_elapsed_since_first_absence(self) -> None:
        window = GraceWindow(span_ns=90)
        assert window.record_absent("0xabc", now_ns=0) is False
        assert window.record_absent("0xabc", now_ns=50) is False
        # Continuously absent past the span: the ghost resolves.
        assert window.record_absent("0xabc", now_ns=91) is True

    def test_fires_exactly_at_the_span_boundary(self) -> None:
        window = GraceWindow(span_ns=90)
        assert window.record_absent("0xabc", now_ns=10) is False
        assert window.record_absent("0xabc", now_ns=100) is True

    def test_a_present_reading_resets_the_grace_clock(self) -> None:
        window = GraceWindow(span_ns=90)
        window.record_absent("0xabc", now_ns=0)
        # The record came back: only *continuous* absence may resolve, so the
        # clock restarts from the next absent reading.
        window.record_present("0xabc")
        assert window.record_absent("0xabc", now_ns=100) is False
        assert window.record_absent("0xabc", now_ns=191) is True

    def test_firing_re_arms_the_window(self) -> None:
        window = GraceWindow(span_ns=90)
        window.record_absent("0xabc", now_ns=0)
        assert window.record_absent("0xabc", now_ns=90) is True
        # Firing cleared the run, so the span is measured afresh from here.
        assert window.record_absent("0xabc", now_ns=100) is False
        assert window.record_absent("0xabc", now_ns=190) is True

    def test_runs_are_independent_per_key(self) -> None:
        window = GraceWindow(span_ns=90)
        window.record_absent("0xabc", now_ns=0)
        # A second cloid arming later keeps its own first-absence stamp.
        assert window.record_absent("0xdef", now_ns=50) is False
        assert window.record_absent("0xdef", now_ns=100) is False
        assert window.record_absent("0xabc", now_ns=100) is True
