"""Property suite: the flatten fill split (ADR-0054, ADR-0056).

A flatten order closes the account net. Its fills must split over every partition. The oracle is
never the split rule itself. It is the fill the test fed and the sizes it started from.

Every test feeds fills one at a time, the way the ``Checkpointer`` does. So the step that turns one
fill into moves is under test too, not only the cumulative rule behind it.
"""

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tickwright.domain import InvariantViolation
from tickwright.engine.flatten_split import PartitionMove, flatten_split

LOT = Decimal("0.1")

Basis = tuple[tuple[str, Decimal], ...]


def _feed(
    basis: Basis, fills: list[Decimal], fees: list[Decimal] | None = None
) -> list[dict[str, PartitionMove]]:
    """Split each fill in order, with the cumulative fill so far, as the engine does."""
    splits = []
    cum_qty = Decimal(0)
    for i, quantity in enumerate(fills):
        cum_qty += quantity
        fee = fees[i] if fees is not None else Decimal(0)
        splits.append(flatten_split(basis, LOT, cum_qty=cum_qty, quantity=quantity, fee=fee))
    return splits


def _sizes(moves: dict[str, PartitionMove]) -> dict[str, Decimal]:
    return {owner: move.size for owner, move in moves.items()}


def test_adr_0056_worked_example() -> None:
    # The example in ADR-0056. The second fill moves only A.
    basis = (("A", Decimal("0.6")), ("B", Decimal("0.6")), ("C", Decimal("0.2")))

    first, second = _feed(basis, [Decimal("1.0"), Decimal("0.1")])

    assert _sizes(first) == {"A": Decimal("-0.4"), "B": Decimal("-0.4"), "C": Decimal("-0.2")}
    assert _sizes(second) == {"A": Decimal("-0.1")}


def test_a_partition_against_the_net_moves_toward_zero_too() -> None:
    # Worked by hand from the ADR-0056 formula. A holds 5 lots long, B 2 lots short. After f lots
    # fill, the long side has moved floor(5 * f / 3) lots and the short side that minus f. A
    # partition the fill does not move is left out.
    basis = (("A", Decimal("0.5")), ("B", Decimal("-0.2")))

    first, second, third = _feed(basis, [LOT, LOT, LOT])

    assert _sizes(first) == {"A": Decimal("-0.1")}
    assert _sizes(second) == {"A": Decimal("-0.2"), "B": Decimal("0.1")}
    assert _sizes(third) == {"A": Decimal("-0.2"), "B": Decimal("0.1")}


@pytest.mark.parametrize(
    ("basis", "fill", "message"),
    [
        ((), LOT, "a basis with no net"),
        ((("A", Decimal("0.1")), ("B", Decimal("-0.1"))), LOT, "a basis with no net"),
        ((("A", Decimal("0.2")),), Decimal("0.3"), "past the basis net"),
    ],
)
def test_a_fill_the_basis_cannot_split_faults(basis: Basis, fill: Decimal, message: str) -> None:
    # The ExecutionManager denies such an order before the send. Reaching the split anyway is a
    # broken caller, and a guess would flip a partition.
    with pytest.raises(InvariantViolation, match=message):
        _feed(basis, [fill])


_ids = st.sampled_from(["A", "B", "C", "D", "E"])


@st.composite
def _basis(draw: st.DrawFn) -> Basis:
    # Partitions may sit on both sides. A zero net has no flatten order, so it is never drawn.
    ids = draw(st.lists(_ids, min_size=1, max_size=5, unique=True))
    lots = draw(
        st.lists(
            st.integers(1, 20).flatmap(lambda n: st.sampled_from([n, -n])),
            min_size=len(ids),
            max_size=len(ids),
        ).filter(lambda sizes: sum(sizes) != 0)
    )
    return tuple((i, n * LOT) for i, n in zip(ids, lots, strict=True))


def _net(basis: Basis) -> Decimal:
    return sum((size for _, size in basis), Decimal(0))


@st.composite
def _book(draw: st.DrawFn) -> tuple[Basis, list[Decimal]]:
    """A basis and fills that close its whole net, cut at random points."""
    basis = draw(_basis())
    total = int(abs(_net(basis)) / LOT)
    cuts = draw(st.sets(st.integers(1, total - 1))) if total > 1 else set()
    edges = [0, *sorted(cuts), total]
    return basis, [(b - a) * LOT for a, b in zip(edges, edges[1:], strict=False)]


@given(book=_book())
def test_a_full_fill_moves_every_partition_to_zero(book: tuple[Basis, list[Decimal]]) -> None:
    basis, fills = book

    splits = _feed(basis, fills)

    # A partition left off zero would open a gap between the books and the venue.
    for owner, size in basis:
        assert (
            size + sum((split[owner].size for split in splits if owner in split), Decimal(0)) == 0
        )


@given(book=_book())
def test_moves_sum_to_the_fill_and_never_pass_zero(book: tuple[Basis, list[Decimal]]) -> None:
    basis, fills = book
    held = dict(basis)

    splits = _feed(basis, fills)

    # A flatten order trades against the net. A sell closes a long net, and a buy a short one.
    direction = -1 if _net(basis) > 0 else 1
    for quantity, split in zip(fills, splits, strict=True):
        assert sum((move.size for move in split.values()), Decimal(0)) == direction * quantity
        for owner, move in split.items():
            start = dict(basis)[owner]
            held[owner] += move.size
            assert held[owner] == 0 or (held[owner] > 0) == (start > 0)


@given(basis=_basis())
def test_a_later_fill_never_takes_a_lot_back(basis: Basis) -> None:
    # One lot per fill, so the split sees every cumulative size. A take-back shows up only at a
    # few of them.
    fills = [LOT] * int(abs(_net(basis)) / LOT)

    splits = _feed(basis, fills)

    # A taken-back lot would book a trade the venue never made. So every move is toward zero.
    start = dict(basis)
    for split in splits:
        for owner, move in split.items():
            assert move.size * start[owner] < 0


def test_a_fee_splits_by_absolute_size() -> None:
    # The second fill moves A by -0.2 and B by +0.1. B moves against the order's side. A split by
    # signed size would hand B a negative fee.
    basis = (("A", Decimal("0.5")), ("B", Decimal("-0.2")))

    _, second = _feed(basis, [LOT, LOT], fees=[Decimal(0), Decimal("0.003")])

    assert {owner: move.fee for owner, move in second.items()} == {
        "A": Decimal("0.002"),
        "B": Decimal("0.001"),
    }


_fee = st.decimals(min_value=-10, max_value=10, places=6, allow_nan=False, allow_infinity=False)


@given(book=_book(), data=st.data())
def test_fee_shares_sum_to_the_fee_and_keep_its_sign(
    book: tuple[Basis, list[Decimal]], data: st.DataObject
) -> None:
    basis, fills = book
    fees = data.draw(st.lists(_fee, min_size=len(fills), max_size=len(fills)))

    splits = _feed(basis, fills, fees)

    for fee, split in zip(fees, splits, strict=True):
        assert sum((move.fee for move in split.values()), Decimal(0)) == fee
        # A rebate stays a rebate.
        assert all(move.fee * fee >= 0 for move in split.values())
