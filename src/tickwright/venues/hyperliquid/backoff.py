"""``Backoff`` — doubling-with-cap reconnect pacing on the injected ``Clock``.

The reconnect loop retries from two places (a refused connect, a venue hangup)
and resets after a good connection; keeping the "sleep the current delay, then
double up to the cap" rule in one value object means those sites can never skew
(ADR-0021). Always slept on the injected ``Clock``, so a reconnect storm can
never hammer the venue and virtual time carries it under ``ManualClock``.
"""

from tickwright.domain import Clock


class Backoff:
    """A doubling delay, capped at ``maximum``, reset to ``initial`` on demand."""

    def __init__(self, *, initial: float, maximum: float) -> None:
        self._initial = initial
        self._maximum = maximum
        self._current = initial

    def reset(self) -> None:
        """Return to the initial delay — called after a good connection."""
        self._current = self._initial

    async def sleep_on(self, clock: Clock) -> None:
        """Sleep the current delay, then double it up to the cap for next time."""
        await clock.sleep(self._current)
        self._current = min(self._current * 2, self._maximum)
