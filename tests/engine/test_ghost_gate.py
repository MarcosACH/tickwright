"""``GhostGate`` — the ADR-0011 inv 3 timing gate for the open-order/ghost cycle.

The gate owns the two-phase decision of when an absent, non-terminal resting
order becomes a ghost: a **recent-order protection** pre-filter (skip while the
last saga event is too fresh — the grace clock never arms) in front of the
**ghost grace window** (continuous absence measured in wall-clock time). It is a
pure decision object — no bus, no clock, no telemetry — so the full inv-3 rule is
exercised here as a state machine, keyed only on the numbers a caller feeds it.
"""

from tickwright.engine.ghost_gate import GhostGate, GhostVerdict


class TestGhostGate:
    def test_a_fresh_order_is_protected(self) -> None:
        gate = GhostGate(grace_span_ns=90, protection_span_ns=30)
        # Last saga event at 0, now 10: inside the protection window → skip.
        assert gate.evaluate("0xabc", now_ns=10, last_event_ns=0) is GhostVerdict.PROTECTED

    def test_the_protection_boundary_is_exclusive(self) -> None:
        gate = GhostGate(grace_span_ns=90, protection_span_ns=30)
        # now - last_event == the span exactly: no longer fresh, evaluation resumes.
        assert gate.evaluate("0xabc", now_ns=30, last_event_ns=0) is GhostVerdict.WAITING

    def test_protection_never_arms_the_grace_clock(self) -> None:
        gate = GhostGate(grace_span_ns=90, protection_span_ns=30)
        # Protected reads while the order is fresh must leave the grace clock alone.
        assert gate.evaluate("0xabc", now_ns=10, last_event_ns=0) is GhostVerdict.PROTECTED
        assert gate.evaluate("0xabc", now_ns=20, last_event_ns=0) is GhostVerdict.PROTECTED
        # It ages out at 40 (40 - 0 >= 30): the grace clock arms *now*, not earlier.
        assert gate.evaluate("0xabc", now_ns=40, last_event_ns=0) is GhostVerdict.WAITING
        # Measured from 40, the ghost is due at 130 — had protection wrongly armed
        # grace back at 10, it would have fired at 100.
        assert gate.evaluate("0xabc", now_ns=129, last_event_ns=0) is GhostVerdict.WAITING
        assert gate.evaluate("0xabc", now_ns=130, last_event_ns=0) is GhostVerdict.GHOST

    def test_unknown_recency_is_never_protected(self) -> None:
        gate = GhostGate(grace_span_ns=90, protection_span_ns=30)
        # No recency (a saga recovered from the store, never checkpointed this
        # session): the startup barrier already re-proved it, so grace is its
        # only guard — it is evaluated straight away.
        assert gate.evaluate("0xabc", now_ns=0, last_event_ns=None) is GhostVerdict.WAITING
        assert gate.evaluate("0xabc", now_ns=90, last_event_ns=None) is GhostVerdict.GHOST

    def test_continuous_absence_across_the_grace_window_ghosts(self) -> None:
        gate = GhostGate(grace_span_ns=90, protection_span_ns=30)
        assert gate.evaluate("0xabc", now_ns=0, last_event_ns=None) is GhostVerdict.WAITING
        assert gate.evaluate("0xabc", now_ns=50, last_event_ns=None) is GhostVerdict.WAITING
        assert gate.evaluate("0xabc", now_ns=90, last_event_ns=None) is GhostVerdict.GHOST

    def test_a_present_reading_resets_the_grace_clock(self) -> None:
        gate = GhostGate(grace_span_ns=90, protection_span_ns=30)
        assert gate.evaluate("0xabc", now_ns=0, last_event_ns=None) is GhostVerdict.WAITING
        # The venue record came back: only *continuous* absence may ghost, so the
        # clock restarts from the next absent reading.
        gate.record_present("0xabc")
        assert gate.evaluate("0xabc", now_ns=100, last_event_ns=None) is GhostVerdict.WAITING
        assert gate.evaluate("0xabc", now_ns=191, last_event_ns=None) is GhostVerdict.GHOST

    def test_ghosting_re_arms_the_window(self) -> None:
        gate = GhostGate(grace_span_ns=90, protection_span_ns=30)
        gate.evaluate("0xabc", now_ns=0, last_event_ns=None)
        assert gate.evaluate("0xabc", now_ns=90, last_event_ns=None) is GhostVerdict.GHOST
        # Firing cleared the run, so the span is measured afresh from here.
        assert gate.evaluate("0xabc", now_ns=100, last_event_ns=None) is GhostVerdict.WAITING
        assert gate.evaluate("0xabc", now_ns=190, last_event_ns=None) is GhostVerdict.GHOST

    def test_runs_are_independent_per_key(self) -> None:
        gate = GhostGate(grace_span_ns=90, protection_span_ns=30)
        gate.evaluate("0xabc", now_ns=0, last_event_ns=None)
        # A second cloid arming later keeps its own first-absence stamp.
        assert gate.evaluate("0xdef", now_ns=50, last_event_ns=None) is GhostVerdict.WAITING
        assert gate.evaluate("0xdef", now_ns=100, last_event_ns=None) is GhostVerdict.WAITING
        assert gate.evaluate("0xabc", now_ns=100, last_event_ns=None) is GhostVerdict.GHOST
