"""The cut part of an order the venue ended short (ADR-0057, #629).

The venue answers ``filled`` for an order it ended with less than the saga
asked for. Two kinds of order end that way. The venue shrinks a reduce-only
order to the position. It drops what an IOC order could not fill. The saga
still asks for the full size, so it would stay open forever. So the adapter
ends the cut part ``CANCELLED``.

Any other order the venue calls ``filled`` filled in full. If its fills read
short, a fill is missing, and ending the saga would lose it.

Placement and reconcile both end a cut part. Each must first prove its fills
read holds every fill, and that proof differs. Placement has the venue's own
``totalSz``. Reconcile has no such figure, so it waits (``CutPartWait``).
"""

from dataclasses import dataclass
from decimal import Decimal

from tickwright.domain import Clock


def cut_part_reason(*, reduce_only: bool, ioc: bool, asked: Decimal, filled: Decimal) -> str | None:
    """Why the cut part of an order the venue calls done ends, or ``None``
    when the order has no cut part.

    ``ioc`` is whether the venue got the order as IOC.
    """
    if filled >= asked:
        return None
    if reduce_only:
        return "reduce-only shrink"
    if ioc:
        return "ioc remainder"
    return None


@dataclass(frozen=True, slots=True)
class _Seen:
    """A fill total an earlier answer showed, and when it first showed it."""

    total: Decimal
    since_ns: int


class CutPartWait:
    """The wait reconcile holds before it ends a cut part.

    One read is not proof the venue has listed every fill yet. A fill that
    lands after ``CANCELLED`` is an illegal move and would be lost. So the cut
    part ends only once the same fill total has shown on every answer for one
    open-order interval. The wait is in clock time, not a read count. A boot
    retry one second later must not pass as a second reconcile pass (#478).

    Kept in memory on purpose. A restart forgets it and only waits once more.
    """

    def __init__(self, *, clock: Clock, wait_ns: int) -> None:
        self._clock = clock
        self._wait_ns = wait_ns
        self._seen: dict[str, _Seen] = {}

    def take(self, cloid: str) -> _Seen | None:
        """Take out what earlier answers showed for ``cloid``.

        Call it on every answer, before any path can return. Then an order
        that stops reading as ended short leaves nothing behind. Only ``held``
        puts it back. A fills read that fails after this restarts the wait.
        That is the safe side, because it only keeps the saga open longer.
        """
        return self._seen.pop(cloid, None)

    def held(self, cloid: str, total: Decimal, *, seen: _Seen | None) -> bool:
        """Whether ``total`` has held for the whole wait.

        ``seen`` is what ``take`` returned for this answer.
        """
        now = self._clock.timestamp_ns()
        if seen is not None and seen.total == total:
            if now - seen.since_ns >= self._wait_ns:
                return True
            self._seen[cloid] = seen
            return False
        # A new total, so the venue was still listing fills. The wait restarts.
        self._seen[cloid] = _Seen(total=total, since_ns=now)
        return False

    def forget(self, cloid: str) -> None:
        """Drop ``cloid`` once its cut part has ended.

        A reconcile read can start the wait while the placement fills read is
        still out. The saga then ends at placement, so no later read would
        take that entry out.
        """
        self._seen.pop(cloid, None)
