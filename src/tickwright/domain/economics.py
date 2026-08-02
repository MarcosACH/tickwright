"""The pure arithmetic each ``Exchange`` adapter runs at its fill boundary (ADR-0036).

A peer of ``quantize_size``/``below_min_notional`` (ADR-0017): a plain function in
``domain`` because the paper adapter and the engine's tests both reach it and
neither may import the other (ADR-0032).

Deliberately **not** a ``FeeModel`` seam. A flat schedule is deterministic config,
not a nondeterminism model like the paper ``FillModel`` (ADR-0012), and such a
seam would be single-implementation on the paper side while live merely reads a
field off the venue's payload. Promoting this to a Protocol is a mechanical
refactor the day a second real implementation exists.
"""

from decimal import Decimal

from .instrument import InstrumentSpec


def fill_fee(*, price: Decimal, quantity: Decimal, maker: bool, spec: InstrumentSpec) -> Decimal:
    """The signed fee a fill of ``quantity`` @ ``price`` incurs — ``notional × rate``.

    ``maker`` selects **which rate** is charged, and that is the whole of what it
    decides: ``> 0`` is a cost debited and ``< 0`` a rebate credited, but making
    liquidity is *not* what makes a fee negative. A negative fee requires a
    maker-rebate volume tier — a property of the account's 14-day volume, not of
    the fill's liquidity side — so on a fresh account a maker fill is a positive
    cost like any other (ADR-0036, observed on testnet in #152). The sign
    therefore rides the configured rate, and this function never imposes one.

    Unrounded on purpose. The venue reports its own fee truncated to 6 dp, but
    that is *its* reporting precision on a number it is the authority for — and
    the live path reads that number verbatim rather than through here. Applying
    it to paper's computed fee would model a rounding rule no paper venue has and
    lose exactness ADR-0029 builds every other figure on.

    ``quantity`` is a magnitude, as it is on the fill it comes from: direction
    lives on the saga's ``side``, and a fee is charged on notional either way.
    """
    return price * quantity * (spec.maker_fee if maker else spec.taker_fee)
