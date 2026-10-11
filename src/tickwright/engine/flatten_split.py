"""``flatten_split`` — split a flatten order's fill over every partition (ADR-0054, ADR-0056).

A flatten order closes the account net, so no single partition owns its fills. The split depends
only on the cumulative fill. That way a fill booked again after a crash gives the same moves.
"""

from collections.abc import Mapping, Sequence
from decimal import Decimal


def flatten_split(
    basis: Sequence[tuple[str, Decimal]], lot_size: Decimal, filled: Decimal
) -> dict[str, Decimal]:
    """Return each partition's total move toward zero after ``filled`` of the order has filled.

    ``basis`` holds each partition's signed size when the order was placed.
    """
    net_long = sum((size for _, size in basis), Decimal(0)) > 0
    with_net = {o: int(abs(s) / lot_size) for o, s in basis if (s > 0) == net_long}
    against = {o: int(abs(s) / lot_size) for o, s in basis if (s > 0) != net_long}
    with_lots, against_lots = sum(with_net.values()), sum(against.values())
    # The order trades only the net, but every partition must reach zero at a full fill. So the
    # side with the net moves faster than the fill, and the side against it moves the difference.
    fill_lots = int(filled / lot_size)
    with_moved = with_lots * fill_lots // (with_lots - against_lots)
    got = _hand_out(with_net, with_moved) | _hand_out(against, with_moved - fill_lots)
    return {owner: (-1 if size > 0 else 1) * got[owner] * lot_size for owner, size in basis}


def split_fee(moves: Mapping[str, Decimal], fee: Decimal) -> dict[str, Decimal]:
    """Split one fill's ``fee`` over the partitions by the absolute size each one moved."""
    # Absolute, not signed. A partition against the net moves the other way, and a signed weight
    # would hand it a negative fee.
    weights = {owner: abs(move) for owner, move in moves.items()}
    total = sum(weights.values(), Decimal(0))
    shares = {owner: fee * weight / total for owner, weight in weights.items()}
    # Division can round. The largest share takes the rounding, so the shares sum to the fee.
    largest = max(sorted(weights), key=lambda o: weights[o])
    shares[largest] += fee - sum(shares.values(), Decimal(0))
    return shares


def _hand_out(lots: dict[str, int], total: int) -> dict[str, int]:
    """Give ``total`` lots one at a time, each to the partition furthest behind its exact share."""
    side_lots = sum(lots.values())
    got = dict.fromkeys(lots, 0)
    # Scaled by the side's size, so the comparison stays in integers. ``max`` keeps the first of
    # equal keys, so a tie goes to the id that sorts first.
    ordered = sorted(lots)
    for handed in range(1, total + 1):
        best = max(ordered, key=lambda o: lots[o] * handed - got[o] * side_lots)
        got[best] += 1
    return got
