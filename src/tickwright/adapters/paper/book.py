"""The paper venue's resting book (ADR-0012): the resting LIMIT orders and the
size still working on each.

Only ``PaperExchange`` uses it. Extracted so the one invariant that ties a
resting order to its remainder lives in a single place instead of across
parallel dicts in the venue: each fill is **capped to what is still working**
(the model may offer more than remains), decremented, and once the remainder is
exhausted the order is **lifted off the book** — so partials converge to exactly
the order size and never over-fill.
"""

from dataclasses import dataclass, replace
from decimal import Decimal

from tickwright.domain import InvariantViolation, PlaceOrder


@dataclass(slots=True)
class _Resting:
    """A resting order, the size the venue works it at, and what has filled.

    ``order`` keeps the size the strategy asked for. ``working`` starts there
    and only goes down, when a reduce-only order shrinks to the position
    (ADR-0057).
    """

    order: PlaceOrder
    working: Decimal
    filled: Decimal = Decimal("0")

    @property
    def remaining(self) -> Decimal:
        return self.working - self.filled


class RestingBook:
    """Resting LIMITs keyed by cloid, each with its working remainder."""

    def __init__(self) -> None:
        self._orders: dict[str, _Resting] = {}

    def rest(self, order: PlaceOrder) -> None:
        """Put ``order`` on the book with its full quantity still working."""
        self._orders[order.cloid] = _Resting(order=order, working=order.quantity)

    def __contains__(self, cloid: object) -> bool:
        return cloid in self._orders

    def resting(self) -> list[PlaceOrder]:
        """A snapshot of the resting orders — safe to fill or remove entries
        (which mutate the book) while iterating over it.

        Each order carries its working size, because the fill model sizes a
        partial from the quantity it is handed.
        """
        return [
            replace(resting.order, quantity=resting.working) for resting in self._orders.values()
        ]

    def shrink(self, cloid: str, remaining: Decimal) -> None:
        """Lower what is still working on a resting order to at most ``remaining``.

        It never raises the size. A reduce-only order that shrank stays shrunk
        when the position grows again (ADR-0057).
        """
        resting = self._orders[cloid]
        resting.working = resting.filled + min(resting.remaining, remaining)

    def shrunk(self, cloid: str) -> bool:
        """Whether a resting order works less than the size the strategy asked for.

        The venue reads it before the fill that completes the order. A shrunk
        order then ends ``CANCELLED`` for its cut part (ADR-0057).
        """
        resting = self._orders[cloid]
        return resting.working < resting.order.quantity

    def apply_fill(self, cloid: str, quantity: Decimal) -> tuple[Decimal, bool]:
        """Fill ``quantity`` against the working remainder of a resting order.

        Caps to what is still working, decrements it, and returns
        ``(filled, complete)``. On completion the order is lifted off the book,
        so a sequence of partials converges to exactly the order size and never
        over-fills. The order must already be resting (fills only land on the
        book), so an unknown ``cloid`` is a broken assumption.
        """
        resting = self._orders.get(cloid)
        if resting is None:
            raise InvariantViolation(f"fill for {cloid} that is not resting on the book")
        filled = min(quantity, resting.remaining)
        resting.filled += filled
        if resting.remaining <= 0:
            del self._orders[cloid]
            return filled, True
        return filled, False

    def has_partial(self, cloid: str) -> bool:
        """Whether a fill has already reduced this order's working remainder.

        The venue reads this to announce ``LIVE`` only for an *untouched*
        resting order — a partial has already driven the saga to a working state.
        """
        resting = self._orders.get(cloid)
        return resting is not None and resting.filled > 0

    def remove(self, cloid: str) -> PlaceOrder | None:
        """Lift a resting order off the book, returning it — or ``None`` if none
        rests under ``cloid`` (already filled/cancelled, or never placed)."""
        resting = self._orders.pop(cloid, None)
        return resting.order if resting is not None else None
