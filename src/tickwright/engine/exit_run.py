"""Operator exit runs (ADR-0052, ADR-0060): one job in place of the strategies.

The ``Engine`` boots and reconciles as usual, then runs an ``ExitJob`` instead of
starting its strategies. The job gets what it needs in an ``ExitContext``,
because the engine builds those parts itself and the composition root never
sees them.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from tickwright.domain import AccountAnchor, VenueOpenOrder, VenueReadFailure


class ExitStatus(StrEnum):
    """How a job ended. Only a final venue read can make it ``DONE`` (ADR-0060)."""

    DONE = "done"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True, kw_only=True)
class ExitOutcome:
    """What a job reports back to the engine when it returns."""

    status: ExitStatus
    left: list[VenueOpenOrder] | VenueReadFailure
    """What the last venue read left, or how that read failed."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ExitContext:
    """The engine's own parts that a job may use, built after the barrier."""

    account: AccountAnchor


class ExitJob(Protocol):
    """One operator job the engine runs in place of its strategies."""

    @property
    def name(self) -> str:
        """The job's name in ``exit.finished``."""
        ...

    async def run(self, context: ExitContext) -> ExitOutcome:
        """Do the job and say what the venue holds at the end."""
        ...


class OperatorCancelAll:
    """``tickwright cancel-all``: cancel every order resting in the account."""

    name = "cancel_all"

    async def run(self, context: ExitContext) -> ExitOutcome:
        left = await context.account.fetch_open_orders()
        return ExitOutcome(status=ExitStatus.DONE, left=left)
