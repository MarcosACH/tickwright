"""The rule for a mark max age, shared by the guard and the reconcile band.

Only the rule is shared. Each keeps its own setting, because they do different
jobs (ADR-0051).
"""

import math

_NS_PER_SECOND = 1_000_000_000


def mark_max_age_ns(seconds: float) -> int:
    """The age in nanoseconds, or ``ValueError`` if it is not a positive number."""
    # Zero or less would call every mark stale. NaN, infinity, and a finite age
    # too large for nanoseconds all load as floats, but ``int()`` cannot convert
    # them. Checking the product catches all three.
    if not (math.isfinite(seconds * _NS_PER_SECOND) and seconds > 0):
        raise ValueError(f"mark_max_age_seconds must be a positive number, got {seconds}")
    return int(seconds * _NS_PER_SECOND)
