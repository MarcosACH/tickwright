"""Property suite: average-cost accounting, against oracles it does not compute.

Every expectation here is derived a *different way* from the reducer — a sum of
signed quantities, a single notional-weighted division, total proceeds minus
total cost — so a property can genuinely disagree with the code rather than
recompute it. What the aggregate does incrementally, one fill at a time, these
oracles do in one shot.
"""

from decimal import Decimal

from hypothesis import example, given
from hypothesis import strategies as st

from tickwright.domain import OrderFilled, OrderFillEvent, Position, Side

_PRICES = st.decimals(
    min_value=Decimal("1"), max_value=Decimal("100000"), places=2, allow_nan=False
)
_QUANTITIES = st.decimals(
    min_value=Decimal("0.001"), max_value=Decimal("1000"), places=3, allow_nan=False
)
_FILLS = st.lists(st.tuples(st.sampled_from(Side), _QUANTITIES, _PRICES), min_size=1, max_size=8)


def _event(index: int, quantity: Decimal, price: Decimal) -> OrderFillEvent:
    return OrderFilled(
        ts_event=1_000,
        ts_init=1_000,
        cloid=f"0x{index}",
        strategy_id="alpha",
        signal_id=f"alpha:BTC:{index}",
        symbol="BTC",
        trade_id=f"f{index}",
        quantity=quantity,
        price=price,
        cum_qty=quantity,
    )


def _fold(fills: list[tuple[Side, Decimal, Decimal]]) -> Position:
    position = Position(strategy_id="alpha", symbol="BTC")
    for index, (side, quantity, price) in enumerate(fills):
        position.apply(_event(index, quantity, price), side=side)
    return position


def _agrees(actual: Decimal, expected: Decimal, *, scale: Decimal) -> bool:
    """Whether two ``Decimal``s agree to within the context's working precision.

    The aggregate folds one fill at a time, rounding at 28 significant digits on
    every weighted-average division; the oracles below divide once, or not at
    all. So the two agree to ~1e-27 relative rather than bit-for-bit, and the
    band below is that with room to spare. ADR-0034's "Tier-1 is exact at venue
    precision" is a claim about venue-quantized quantities, not about an
    arbitrary generated fold.

    ``scale`` is the caller's — the magnitude of the *terms* the fold summed,
    never of the value it arrived at. The rounding error is inherited from the
    intermediates, so a result that cancelled down to a fraction of them (a
    round trip closed near its entry realizes cents out of tens of thousands)
    carries an error the result's own magnitude cannot pay for (#207). There is
    no defensible default here: only the caller knows what its oracle summed.
    """
    return abs(actual - expected) <= max(scale, Decimal(1)) * Decimal("1e-24")


@given(fills=_FILLS)
def test_signed_size_is_the_sum_of_the_signed_fills(
    fills: list[tuple[Side, Decimal, Decimal]],
) -> None:
    """Size is linear in the fills and blind to every regime the reducer picks —
    open, add, reduce, close and flip alike. Exact, and the bridge the
    Σ-invariant over partitions is built on (ADR-0034/0038)."""
    expected = sum((q if side is Side.BUY else -q for side, q, _price in fills), start=Decimal("0"))

    assert _fold(fills).signed_size == expected


@given(
    fills=st.lists(st.tuples(_QUANTITIES, _PRICES), min_size=1, max_size=6),
    side=st.sampled_from(Side),
    permutation=st.randoms(use_true_random=False),
)
def test_average_entry_is_the_weighted_mean_whatever_order_the_fills_arrive_in(
    fills: list[tuple[Decimal, Decimal]], side: Side, permutation: object
) -> None:
    """Accumulating on one side, the entry is Σ(price × qty) / Σ qty — one
    division, computed here without touching the reducer — and reordering the
    same fills cannot move it."""
    expected = sum((p * q for q, p in fills), start=Decimal("0")) / sum(
        (q for q, _p in fills), start=Decimal("0")
    )
    shuffled = list(fills)
    permutation.shuffle(shuffled)  # type: ignore[attr-defined]

    entry = _fold([(side, q, p) for q, p in fills]).entry_price
    reordered = _fold([(side, q, p) for q, p in shuffled]).entry_price

    # Every term the weighted mean divides is a price, so the dearest of them
    # bounds the magnitude the fold's divisions round against.
    scale = max(p for _q, p in fills)

    assert _agrees(entry, expected, scale=scale)
    assert _agrees(reordered, entry, scale=scale)


@example(
    fills=[
        (Side.SELL, Decimal("0.002"), Decimal("1.00")),
        (Side.SELL, Decimal("0.541"), Decimal("16113.91")),
        (Side.BUY, Decimal("0.541"), Decimal("16107.44")),
    ]
)
@given(fills=_FILLS)
def test_a_sequence_that_ends_flat_realizes_proceeds_minus_cost(
    fills: list[tuple[Side, Decimal, Decimal]],
) -> None:
    """A position that opens from flat and returns to flat has realized exactly
    what it took in less what it paid out — whatever path it took through adds,
    reduces and flips. The oracle knows nothing of average cost.

    Appending the closing fill is what makes the case reachable at all: a
    generated sequence lands flat too rarely to test by filtering.

    The pinned example is #207's: a round trip whose two legs cancel from
    ~8.7e3 down to ~1.9, so the answer is ~4000× smaller than the terms that
    produced it. It rides in the source because ``.hypothesis/`` is gitignored
    and CI would otherwise never see it.
    """
    net = sum((q if side is Side.BUY else -q for side, q, _p in fills), start=Decimal("0"))
    closing_price = Decimal("777.77")
    sequence = list(fills)
    if net != 0:
        sequence.append((Side.SELL if net > 0 else Side.BUY, abs(net), closing_price))

    position = _fold(sequence)
    proceeds = sum(
        ((q * p if side is Side.SELL else -q * p) for side, q, p in sequence),
        start=Decimal("0"),
    )

    # Gross notional, not the netted result: the fold's error is carried by the
    # legs, and the two can differ by orders of magnitude when they cancel.
    gross = sum((abs(q * p) for _side, q, p in sequence), start=Decimal("0"))

    assert position.is_flat
    assert position.entry_price == Decimal("0")
    assert _agrees(position.realized_pnl, proceeds, scale=gross)


@given(fills=_FILLS, redelivery=st.randoms(use_true_random=False))
def test_redelivering_applied_fills_changes_no_outcome(
    fills: list[tuple[Side, Decimal, Decimal]], redelivery: object
) -> None:
    """At-least-once delivery converges: replaying any subset of already-applied
    fills, in any order, leaves every Tier-1 line where it was (ADR-0025)."""
    position = _fold(fills)
    settled = (position.signed_size, position.entry_price, position.realized_pnl)

    replay = [
        (index, side, q, p)
        for index, (side, q, p) in enumerate(fills)
        if redelivery.random() < 0.5  # type: ignore[attr-defined]
    ]
    redelivery.shuffle(replay)  # type: ignore[attr-defined]
    for index, side, quantity, price in replay:
        assert position.apply(_event(index, quantity, price), side=side) == ()

    assert (position.signed_size, position.entry_price, position.realized_pnl) == settled
