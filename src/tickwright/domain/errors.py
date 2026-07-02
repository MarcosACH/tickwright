"""Domain error taxonomy.

An ``InvariantViolation`` is the fail-fast class (ADR-0014): an illegal saga
transition or a broken engine assumption. It is never a handler error — it
pierces containment and faults the engine rather than being logged-and-skipped.
"""


class InvariantViolation(Exception):
    """A load-bearing engine invariant was broken (fail-fast, ADR-0014)."""
