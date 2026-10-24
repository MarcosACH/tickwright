"""The wake members for a test strategy that never wakes (ADR-0062)."""

from tickwright.domain import Wake


class NeverWakes:
    """Gives a test strategy the two wake members of ``Strategy``, set to never wake.

    Only members with one safe default belong here. ``set_next_seq`` and
    ``snapshot`` stay in each fake: a fake that sends signals must resume its
    seq, and a wrong default would hide that.

    The strategies in ``user_strategies`` stay without it. They show the
    contract the way a user writes it, and some leave a member out on purpose.
    """

    wake_interval_seconds: float | None = None

    async def on_wake(self, wake: Wake) -> None:
        pass
