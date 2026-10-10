"""The paper venue's own fills that the store has not applied yet (ADR-0057).

Only ``PaperExchange`` uses it. Paper publishes a fill from inside a bus
handler, and the store applies it only after that handler returns. Until then,
the store net misses the fill. So the venue adds these fills on top of the
store net, or two quick reduce-only orders could both close the same position.

A fill is dropped once the store shows it applied. It is checked against the
store, never against the saga's fill event, because the in-memory bus can
deliver a new order before that event.
"""

from collections.abc import Callable, Collection
from decimal import Decimal

from tickwright.domain import FillReport, Side


class UnappliedFills:
    """Paper's own fills, each with the side of its order, until the store
    applies them."""

    def __init__(self, applied: Callable[[str], Collection[str]]) -> None:
        # The fill ids the store has applied to one order.
        self._applied = applied
        self._fills: list[tuple[FillReport, Side]] = []

    def record(self, fill: FillReport, side: Side) -> None:
        """Count ``fill`` until the store applies it."""
        self._fills.append((fill, side))

    def signed_size(self, symbol: str) -> Decimal:
        """The signed size the store has not applied yet in ``symbol``.

        It first drops every fill the store has applied. Counting one of those
        would count it twice.
        """
        self.drain()
        size = Decimal("0")
        for fill, side in self._fills:
            if fill.symbol == symbol:
                size += fill.quantity if side is Side.BUY else -fill.quantity
        return size

    def drain(self) -> None:
        """Drop every fill the store has applied."""
        self._fills = [
            (fill, side)
            for fill, side in self._fills
            if fill.event_id not in self._applied(fill.cloid)
        ]
