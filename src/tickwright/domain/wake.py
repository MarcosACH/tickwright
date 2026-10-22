"""The rule for a strategy's wake interval (ADR-0062)."""

import math


def wake_interval_refusal(interval: object) -> str | None:
    """The sentence refusing a wake interval, or ``None`` if the interval is fine.

    The strategy loader and ``StrategyHost.register`` both refuse a bad interval, each with its
    own exception type. The rule and its wording live here, so the two gates cannot drift apart.
    """
    if interval is None:
        return None
    # bool is an int to Python, so True would otherwise pass as 1 second.
    if isinstance(interval, bool) or not isinstance(interval, int | float):
        return f"wake_interval_seconds must be a finite number of seconds or None, not {interval!r}"
    # NaN and infinity both pass a bare "< 1". An infinite interval never wakes.
    if not math.isfinite(interval):
        return f"wake_interval_seconds must be a finite number of seconds or None, not {interval!r}"
    if interval < 1:
        return f"wake_interval_seconds must be at least 1 second, not {interval!r}"
    return None
