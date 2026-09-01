"""``StartupBarrier`` — the hard startup gate (ADR-0024): nothing places until
every proof it holds has cleared.

The gate is a *lifecycle* concept rather than a reconciliation one, which is why
it is its own type: it drives steps from more than one owner, and what it owns is
none of them. It owns the **policy they share**: drive the steps in order, retry
the whole attempt with capped backoff when one freezes, and fault on the
deadline. The runner composes the sequence, so the ordering — and the reasons a
particular order is load-bearing — stays in the one place lifecycle ordering
lives (``runner.py``), rather than being smeared into whichever component happens
to run first. **This type therefore never names a step**: it knows their shape
and their order, and nothing about what any of them proves.

A step reports ``bool``: ``True`` means it cleared, ``False`` means it could not
prove what it was asked to and **guessed nothing** — ADR-0011 inv 1's
freeze-don't-guess, in the return type. A step that raises is not a freeze but a
broken assumption, and rides out to the runner unchanged.

**Steps are re-driven from the top on every attempt**, so each must be safe to
repeat — a step that has already cleared is expected to find nothing left to do.
"""

from collections.abc import Awaitable, Callable

from tickwright.domain import Backoff, Clock, Deadline, StartupReconciliationTimeout

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
        non-zero exit for the external supervisor to backoff-restart. A sequence
        whose steps cannot fail clears on the first attempt and pays none of
        this; whether a given composition is one of those is the caller's fact,
        not this type's.

        **Clearing with a step unproved is not an available outcome**, which is
        the whole of what the gate is for: a venue that will not answer stops the
        engine rather than being read as an empty book or an unopened ledger.

        The pacing is ``domain``'s ``Backoff`` and the window is its
        ``Deadline`` — the same two values the venue adapters' boot guards run
        on, rather than the arithmetic this method used to write for itself. The
        cap that ``Backoff`` carries is what keeps an uncapped doubling from
        carrying the clock far past the deadline: without it a large
        ``timeout_seconds`` would fault nearly a whole backoff interval late,
        making real time-to-``FAULTED`` up to ~2× the configured window.

        What this loop keeps is the part no other caller shares: **a freeze is a
        ``False`` return, not an exception**. That is ADR-0011 inv 1 in the step
        contract, and it is why the shared primitives stop at pacing and a
        window — hoisting the loop itself would have to name what counts as a
        retryable failure, which is different for every caller.
        """
        deadline = Deadline.opening(clock=self._clock, budget_seconds=timeout_seconds)
        backoff = Backoff(initial=initial_backoff_seconds, maximum=max_backoff_seconds)
        while not await self._attempt():
            if deadline.spent(self._clock):
                raise StartupReconciliationTimeout(
                    f"venue unreachable for {timeout_seconds}s during startup "
                    "reconciliation; refusing to start on unverified state"
                )
            await backoff.sleep_on(self._clock)

    async def _attempt(self) -> bool:
        """One pass over the steps, in order, stopping at the first freeze.

        Stopping is load-bearing rather than an optimization: the sequence is
        ordered because a later step may depend on an earlier one having landed,
        so running the tail behind a freeze would be running it against state its
        predecessor could not prove. Which dependence, and what it costs to get
        it wrong, is the composing caller's to state (``runner.py``).
        """
        for step in self._steps:
            if not await step():
                return False
        return True
