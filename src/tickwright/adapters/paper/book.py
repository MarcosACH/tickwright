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
from typing import NamedTuple

from tickwright.domain import InvariantViolation, PlaceOrder


class BookFill(NamedTuple):
    """What one fill did to a resting order."""

    filled: Decimal
    complete: bool
    # True only on the fill that completes an order working below the size it
    # asked for. That fill lifts the order off the book, so it is the last
    # moment the book can say so (ADR-0057).
    shrunk: bool


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

    def at_working_size(self) -> PlaceOrder:
        """The order at its working size. The fill model sizes a partial from
        the quantity it is handed, so it must never see the asked size."""
        return replace(self.order, quantity=self.working)


class RestingBook:
    """Resting LIMITs keyed by cloid, each with its working remainder."""

    def __init__(self) -> None:
        self._orders: dict[str, _Resting] = {}

    def rest(self, order: PlaceOrder, *, working: Decimal | None = None) -> PlaceOrder:
        """Put ``order`` on the book and return it at the size the book works.

        ``working`` is that size, by default the asked size. It is capped at
        the asked size, because working more would over-fill the order.
        """
        if working is None:
            working = order.quantity
        resting = _Resting(order=order, working=min(working, order.quantity))
        self._orders[order.cloid] = resting
        return resting.at_working_size()

    def working(self, cloid: str) -> PlaceOrder | None:
        """One resting order at its working size now, or ``None`` if it has
        left the book."""
        resting = self._orders.get(cloid)
        return resting.at_working_size() if resting is not None else None

    def resting(self) -> list[PlaceOrder]:
        """A snapshot of the resting orders at their working size — safe to
        fill or remove entries (which mutate the book) while iterating over it.
        """
        return [resting.at_working_size() for resting in self._orders.values()]

    def shrink(self, cloid: str, remaining: Decimal) -> None:
        """Lower what is still working on a resting order to at most ``remaining``.

        It never raises the size. A reduce-only order that shrank stays shrunk
        when the position grows again (ADR-0057).
        """
        resting = self._orders[cloid]
        resting.working = resting.filled + min(resting.remaining, remaining)

    def apply_fill(self, cloid: str, quantity: Decimal) -> BookFill:
        """Fill ``quantity`` against the working remainder of a resting order.

        Caps to what is still working and decrements it. On completion the
        order is lifted off the book, so a sequence of partials converges to
        exactly the order size and never over-fills. A shrunk order ends
        ``CANCELLED`` for its cut part, so the completing fill says whether it
        was shrunk (ADR-0057). The order must already be resting (fills only
        land on the book), so an unknown ``cloid`` is a broken assumption.
        """
        resting = self._orders.get(cloid)
        if resting is None:
            raise InvariantViolation(f"fill for {cloid} that is not resting on the book")
        filled = min(quantity, resting.remaining)
        resting.filled += filled
        if resting.remaining <= 0:
            del self._orders[cloid]
            return BookFill(filled, True, resting.working < resting.order.quantity)
        return BookFill(filled, False, False)

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
