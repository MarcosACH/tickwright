"""The startup barrier: an ordered sequence of proofs inside one retry budget.

The hard gate of ADR-0024 — nothing places until it clears — moved off the
``Reconciler`` because it now drives two steps from two owners: the live-only
account materialisation (ADR-0043 §6) and ADR-0011's order mass-rebuild, in that
order. What it owns is the *policy* they share: retry with capped backoff, then
``StartupReconciliationTimeout`` for the runner to map to ``FAULTED``.
"""

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from tickwright.adapters.clock import ManualClock
from tickwright.domain import StartupReconciliationTimeout
from tickwright.engine.barrier import StartupBarrier


def _step(trail: list[str], name: str, *, clears: bool) -> Callable[[], Awaitable[bool]]:
    """A barrier step that records that it ran and returns its verdict."""

    async def run() -> bool:
        trail.append(name)
        return clears

    return run


def test_a_frozen_step_stops_the_attempt_before_the_steps_behind_it() -> None:
    """The order is load-bearing, not presentational (ADR-0043 §6): the account
    materialisation runs *before* the mass-rebuild, and a rebuild that ran
    anyway behind a frozen account read would emit synthetic fills whose own
    ledger write creates the account row — at the zero ``Account.open`` resolves
    a ``None`` genesis to, with nothing left to refuse it afterwards.
    """
    trail: list[str] = []
    barrier = StartupBarrier(
        clock=ManualClock(start_ns=0),
        steps=(_step(trail, "account", clears=False), _step(trail, "orders", clears=True)),
    )

    with pytest.raises(StartupReconciliationTimeout):
        asyncio.run(barrier.run(timeout_seconds=30.0))

    assert set(trail) == {"account"}
    assert len(trail) > 1, "the gate must retry rather than fault on the first freeze"
