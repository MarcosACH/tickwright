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


_ids = st.sampled_from(["A", "B", "C", "D", "E"])


@st.composite
def _one_sided_basis(draw: st.DrawFn) -> tuple[tuple[str, Decimal], ...]:
    ids = draw(st.lists(_ids, min_size=1, max_size=5, unique=True))
    sign = draw(st.sampled_from([1, -1]))
    return tuple((i, sign * draw(st.integers(1, 20)) * LOT) for i in ids)


@given(basis=_one_sided_basis(), data=st.data())
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
