"""The rule for an engine setting given in seconds.

One rule decides which values each such setting can take. A bad value then
stops the boot the same way for all of them.
"""

import math

_NS_PER_SECOND = 1_000_000_000


def duration_ns(seconds: float, *, name: str) -> int:
    """``seconds`` in nanoseconds, or ``ValueError`` naming ``name`` if it is not
    a positive number."""
    # Zero or less makes no sense for any of these settings, and neither does a
    # value under one nanosecond, which rounds to zero. A zero interval never
    # waits, and a zero grace window gives up on the first read. NaN, infinity,
    # and a finite value too large for nanoseconds all load as floats, but
    # ``int()`` cannot convert them. Checking the product catches every case.
    ns = seconds * _NS_PER_SECOND
    if not (math.isfinite(ns) and ns >= 1):
        raise ValueError(
            f"{name} must be a positive number of at least 1e-9 seconds, got {seconds}"
        )
    return int(ns)
