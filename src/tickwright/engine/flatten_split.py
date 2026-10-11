"""``flatten_split`` — split a flatten order's fill over every partition (ADR-0054, ADR-0056).

A flatten order closes the account net, so no single partition owns its fills. The split depends
only on the cumulative fill. That way a fill booked again after a crash gives the same moves.
"""

from collections.abc import Sequence
from decimal import Decimal


def flatten_split(
    basis: Sequence[tuple[str, Decimal]], lot_size: Decimal, filled: Decimal
) -> dict[str, Decimal]:
    """Return each partition's total move toward zero after ``filled`` of the order has filled.

    ``basis`` holds each partition's signed size when the order was placed.
    """
    lots = {owner: int(abs(size) / lot_size) for owner, size in basis}
    side_lots = sum(lots.values())
    got = dict.fromkeys(lots, 0)
    # One lot at a time, to the partition furthest behind its exact share. Scaled by the side's
    # size, so the comparison stays in integers. ``max`` keeps the first of equal keys, so a tie
    # goes to the id that sorts first.
    ordered = sorted(lots)
    for handed in range(1, int(filled / lot_size) + 1):
        best = max(ordered, key=lambda o: lots[o] * handed - got[o] * side_lots)
        got[best] += 1
    return {owner: (-1 if size > 0 else 1) * got[owner] * lot_size for owner, size in basis}
