"""The pure arithmetic each ``Exchange`` adapter runs at its economic boundaries —
the fill's fee (ADR-0036) and the funding boundary's accrual (ADR-0037).

Peers of ``quantize_size``/``below_min_notional`` (ADR-0017): plain functions in
``domain`` because the paper adapter and the engine's tests both reach them and
neither may import the other (ADR-0032).

Deliberately **neither** a ``FeeModel`` nor a ``FundingModel`` seam. A flat
schedule is deterministic config, not a nondeterminism model like the paper
``FillModel`` (ADR-0012), and such a seam would be single-implementation on the
paper side while live merely reads a field off the venue's payload. Promoting
either to a Protocol is a mechanical refactor the day a second real
implementation exists.
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


def funding_amount(*, signed_size: Decimal, price: Decimal, spec: InstrumentSpec) -> Decimal:
    """The signed funding a position of ``signed_size`` accrues at one boundary.

    ``− signed_size × price × funding_rate`` (ADR-0037), and the **leading minus
    is the whole of what this function decides**: it is what makes the result
    mirror the venue's ``userFunding.usdc``, where ``< 0`` is funding paid and
    ``> 0`` funding received. A long at a positive rate pays, a short at the same
    rate receives, and both flip when the rate is negative — so the sign rides
    the size and the rate together, and the account applies ``cash += amount``
    with no transformation of its own.

    ``signed_size`` is **signed** where ``fill_fee``'s ``quantity`` is a
    magnitude, and the asymmetry is real rather than an inconsistency: a fee is
    charged on notional whichever way the trade went, while which side of a
    funding payment you are on *is* the direction of the position.

    ``price`` is the caller's notional basis. Paper passes its one price signal,
    the last trade, collapsing the venue's oracle/mark distinction — a
    distinction that only bites where funding is computed against a real venue,
    which live never does (it ingests). Unrounded for the same reason the fee is.
    """
    return -signed_size * price * spec.funding_rate


def funding_boundaries(*, after_ns: int, through_ns: int, interval_ns: int) -> tuple[int, ...]:
    """The epoch-aligned funding boundaries strictly crossed in ``(after, through]``.

    A boundary is any instant that is an integer multiple of ``interval_ns``
    **measured from the Unix epoch** (ADR-0037), which at the default hour is
    exactly the top of each UTC hour — the venue's real schedule. Measuring from
    the epoch rather than from the run's start is what makes a replayed run cross
    the *same absolute* boundaries a live one would; a start-relative phase would
    be arbitrary and irreproducible across launches.

    **Every** boundary in the span is returned, ascending, and that is the whole
    point of enumerating them rather than answering "has one passed?": funding is
    **additive** where the reconcile cadences are convergent (ADR-0033), so a
    virtual-time jump across N boundaries is N distinct payments and collapsing
    them to one would silently under-charge.

    Half-open — ``after`` exclusive, ``through`` inclusive — so consecutive spans
    tile the timeline exactly once. A caller that has just settled ``t`` passes it
    as ``after_ns`` and is never handed it again, while an instant that lands
    *on* a boundary has crossed it and settles it now rather than next time.
    """
    first = after_ns // interval_ns + 1
    last = through_ns // interval_ns
    return tuple(index * interval_ns for index in range(first, last + 1))
