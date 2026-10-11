"""Property suite: the flatten fill split (ADR-0054, ADR-0056).

A flatten order closes the account net. Its fills must split over every partition. The oracle is
never the split rule itself. It is the fill the test fed and the sizes it started from.
"""

from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from tickwright.engine.flatten_split import flatten_split

LOT = Decimal("0.1")


def test_adr_0056_worked_example() -> None:
    # The example in ADR-0056. The second fill moves only A.
    basis = (("A", Decimal("0.6")), ("B", Decimal("0.6")), ("C", Decimal("0.2")))

    assert flatten_split(basis, LOT, Decimal("1.0")) == {
        "A": Decimal("-0.4"),
        "B": Decimal("-0.4"),
        "C": Decimal("-0.2"),
    }
    assert flatten_split(basis, LOT, Decimal("1.1")) == {
        "A": Decimal("-0.5"),
        "B": Decimal("-0.4"),
        "C": Decimal("-0.2"),
    }


def test_a_partition_against_the_net_moves_toward_zero_too() -> None:
    # Worked by hand from the ADR-0056 formula. A holds 5 lots long, B 2 lots short. After f lots
    # fill, the long side has moved floor(5 * f / 3) lots and the short side that minus f.
    basis = (("A", Decimal("0.5")), ("B", Decimal("-0.2")))

    assert flatten_split(basis, LOT, Decimal("0.1")) == {"A": Decimal("-0.1"), "B": Decimal("0")}
    assert flatten_split(basis, LOT, Decimal("0.2")) == {
        "A": Decimal("-0.3"),
        "B": Decimal("0.1"),
    }
    assert flatten_split(basis, LOT, Decimal("0.3")) == {
        "A": Decimal("-0.5"),
        "B": Decimal("0.2"),
    }


_ids = st.sampled_from(["A", "B", "C", "D", "E"])


@st.composite
def _basis(draw: st.DrawFn) -> tuple[tuple[str, Decimal], ...]:
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


@given(basis=_basis())
def test_a_full_fill_moves_every_partition_to_zero(basis: tuple[tuple[str, Decimal], ...]) -> None:
    net = sum((size for _, size in basis), Decimal(0))

    moves = flatten_split(basis, LOT, abs(net))

    # A partition left off zero would open a gap between the books and the venue.
    assert all(size + moves[owner] == 0 for owner, size in basis)


@given(basis=_basis(), data=st.data())
def test_moves_sum_to_the_fill_and_never_pass_zero(
    basis: tuple[tuple[str, Decimal], ...], data: st.DataObject
) -> None:
    net = sum((size for _, size in basis), Decimal(0))
    filled = data.draw(st.integers(0, int(abs(net) / LOT))) * LOT

    moves = flatten_split(basis, LOT, filled)

    # A flatten order trades against the net. A sell closes a long net, and a buy a short one.
    expected = -filled if net > 0 else filled
    assert sum(moves.values(), Decimal(0)) == expected
    for owner, size in basis:
        after = size + moves[owner]
        assert after == 0 or (after > 0) == (size > 0)


@given(basis=_basis())
def test_a_later_fill_never_takes_a_lot_back(basis: tuple[tuple[str, Decimal], ...]) -> None:
    net = sum((size for _, size in basis), Decimal(0))
    # Every fill size, not two random ones. A take-back shows up only at a few fill sizes.
    fills = [lots * LOT for lots in range(int(abs(net) / LOT) + 1)]

    splits = [flatten_split(basis, LOT, filled) for filled in fills]

    # A taken-back lot would book a trade the venue never made.
    for before, after in zip(splits, splits[1:], strict=False):
        for owner, _ in basis:
            assert abs(after[owner]) >= abs(before[owner])
