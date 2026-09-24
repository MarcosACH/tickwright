"""The rule for an engine setting given in seconds.

Every such setting becomes nanoseconds on the engine clock. One rule decides
which values can, so a bad value stops the boot the same way for each of them.
"""

import math

_NS_PER_SECOND = 1_000_000_000


def duration_ns(seconds: float, *, name: str) -> int:
    """``seconds`` in nanoseconds, or ``ValueError`` naming ``name`` if it is not
    a positive number."""
    # Zero or less makes no sense for any of these settings. NaN, infinity, and
    # a finite value too large for nanoseconds all load as floats, but ``int()``
    # cannot convert them. Checking the product catches all three.
    if not (math.isfinite(seconds * _NS_PER_SECOND) and seconds > 0):
        raise ValueError(f"{name} must be a positive number, got {seconds}")
    return int(seconds * _NS_PER_SECOND)
