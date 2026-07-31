"""``StartupBarrier`` — the hard startup gate (ADR-0024): nothing places until
every proof it holds has cleared.

The gate is a *lifecycle* concept rather than a reconciliation one, which is why
it is its own type: it drives two steps from two owners — the live-only account
materialisation (ADR-0043 §6) and ADR-0011's order mass-rebuild — and what it
owns is neither of them. It owns the **policy they share**: drive the steps in
order, retry the whole attempt with capped backoff when one freezes, and fault
on the deadline. The runner composes the sequence, so the ordering stays in the
one place lifecycle ordering lives (``runner.py``'s module docstring) rather
than being smeared into whichever component happens to run first.

A step reports ``bool``: ``True`` means it cleared, ``False`` means it could not
prove what it was asked to and **guessed nothing** — ADR-0011 inv 1's
freeze-don't-guess, which every step here shares because every one of them reads
the venue. A step that raises is not a freeze but a broken assumption, and rides
out to the runner unchanged.

**Steps are re-driven from the top on every attempt**, so each must be safe to
repeat: a step that has already cleared has by construction nothing left to do
(the account row exists; the sagas already reconciled converge under dedup).
"""

from collections.abc import Awaitable, Callable

from tickwright.domain import Clock, StartupReconciliationTimeout

_NS_PER_SECOND = 1_000_000_000

BarrierStep = Callable[[], Awaitable[bool]]
"""One proof the gate holds: ``True`` cleared, ``False`` froze (never guessed)."""


class StartupBarrier:
    """The ordered proofs startup is gated on, and the retry policy they share."""

    def __init__(self, *, clock: Clock, steps: tuple[BarrierStep, ...]) -> None:
        self._clock = clock
        self._steps = steps

    async def run(
        self,
        *,
        timeout_seconds: float,
        initial_backoff_seconds: float = 1.0,
        max_backoff_seconds: float = 30.0,
    ) -> None:
        """Drive every step to a clear, or fault (ADR-0024).

        Retries with exponential backoff so a transient boot-time venue blip
        resolves and startup proceeds; a sustained outage trips
        ``startup_reconciliation_timeout`` → ``StartupReconciliationTimeout``
        (an ``InvariantViolation``), which the runner maps to ``FAULTED`` and a
        non-zero exit for the external supervisor to backoff-restart. On the
        paper path no step can fail, so the barrier always clears.

        **Clearing with a step unproved is not an available outcome**, which is
        the whole of what the gate is for: a venue that will not answer stops the
        engine rather than being read as an empty book or an unopened ledger.

        The backoff is capped at ``max_backoff_seconds`` so an uncapped doubling
        cannot carry the clock far past the deadline: without the cap a large
        ``timeout_seconds`` would fault nearly a whole backoff interval late,
        making real time-to-``FAULTED`` up to ~2× the configured window.
        """
        deadline_ns = self._clock.timestamp_ns() + int(timeout_seconds * _NS_PER_SECOND)
        backoff_seconds = initial_backoff_seconds
        while not await self._attempt():
            if self._clock.timestamp_ns() >= deadline_ns:
                raise StartupReconciliationTimeout(
                    f"venue unreachable for {timeout_seconds}s during startup "
                    "reconciliation; refusing to start on unverified state"
                )
            await self._clock.sleep(backoff_seconds)
            backoff_seconds = min(backoff_seconds * 2, max_backoff_seconds)

    async def _attempt(self) -> bool:
        """One pass over the steps, in order, stopping at the first freeze.

        Stopping is load-bearing rather than an optimization: the steps are
        ordered because a later one depends on an earlier having landed —
        ADR-0043 §6's rebuild would *create* the live account row itself, at a
        zero genesis, if it ran behind a materialisation that could not read the
        venue.
        """
        for step in self._steps:
            if not await step():
                return False
        return True
