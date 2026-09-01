"""``Backoff`` — doubling-with-cap retry pacing on the injected ``Clock``.

Two loops in this package pace a retry, and keeping the "sleep the current
delay, then double up to the cap" rule in one value object means they cannot
skew. ``WsSession`` — the reconnect loop both live subscriptions run on —
retries from two places (a refused connect, a venue hangup) and ``reset``s after
a good connection (ADR-0021). The boot
guards' account-mode read retries an unreadable response and never resets:
it has one bounded window in which to clear or refuse, so there is no "good
connection" to return to (ADR-0046 §3).

Always slept on the injected ``Clock``, so a retry storm can never hammer the
venue and virtual time carries it under ``ManualClock``.
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
