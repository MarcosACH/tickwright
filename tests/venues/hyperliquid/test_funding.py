"""Live funding ingest — the venue's own payments, taken verbatim (ADR-0037).

The expected amounts here are the venue's **reported field**, not a number this
suite recomputed: `-3.625312` is quoted from the `userFunding` example in
`docs/research/hyperliquid-perp-fees-funding.md`, whose worked case is a long
`szi=+49.1477` at rate `+0.0000417`. Re-deriving it with paper's formula would
make the test agree with the code by construction and leave a sign flip
invisible, which is the one bug this module exists not to have.
"""

from decimal import Decimal

from tickwright.domain import FundingAccrual
from tickwright.venues.hyperliquid.funding import accruals

_NS_PER_MS = 1_000_000


def funding(*, time_ms: int, coin: str, usdc: str, szi: str = "1", rate: str = "0.0000417") -> dict:
    """One `WsUserFunding` record, the venue's flat per-payment shape."""
    return {"time": time_ms, "coin": coin, "usdc": usdc, "szi": szi, "fundingRate": rate}


def test_a_reported_payment_becomes_an_accrual_with_the_venue_s_own_amount_and_time() -> None:
    reported = funding(time_ms=1681222254710, coin="ETH", usdc="-3.625312", szi="49.1477")

    (accrual,) = accruals([reported], account_id="hyperliquid-mainnet-0xabc", ts_init_ns=42)

    assert isinstance(accrual, FundingAccrual)
    assert accrual.account_id == "hyperliquid-mainnet-0xabc"
    assert accrual.symbol == "ETH"
    # Verbatim, sign included: negative is funding paid (ADR-0037 §Sign).
    assert accrual.amount == Decimal("-3.625312")
    assert accrual.boundary_ts_ns == 1681222254710 * _NS_PER_MS
    assert accrual.ts_event == 1681222254710 * _NS_PER_MS
    assert accrual.ts_init == 42


def test_a_received_payment_keeps_the_venue_s_positive_sign() -> None:
    """The other half of the sign, so a `abs()` or a negation cannot pass.

    A short at a positive rate is paid, and the venue reports that as a positive
    `usdc`. Nothing on this path may transform it.
    """
    reported = funding(time_ms=1681225854710, coin="BTC", usdc="3.625312", szi="-49.1477")

    (accrual,) = accruals([reported], account_id="hyperliquid-mainnet-0xabc", ts_init_ns=42)

    assert accrual.amount == Decimal("3.625312")


def test_an_out_of_order_batch_is_emitted_in_venue_time_order() -> None:
    """The module's one piece of real content (ADR-0043 §5.2, module map).

    The venue documents no delivery order within a batch, and the projection's
    watermark gate is monotonic: fed `t3, t1, t2` unsorted it would apply `t3`,
    advance the mark past it, then drop `t1` and `t2` as already-applied. Those
    are real payments the account never sees again — the snapshot is delivered
    once. Sorting here is the only place that ordering can be established.
    """
    shuffled = [
        funding(time_ms=1681229454710, coin="ETH", usdc="-1"),
        funding(time_ms=1681222254710, coin="ETH", usdc="-3"),
        funding(time_ms=1681225854710, coin="ETH", usdc="-2"),
    ]

    emitted = accruals(shuffled, account_id="hyperliquid-mainnet-0xabc", ts_init_ns=42)

    assert [a.boundary_ts_ns for a in emitted] == [
        1681222254710 * _NS_PER_MS,
        1681225854710 * _NS_PER_MS,
        1681229454710 * _NS_PER_MS,
    ]
    # Each amount still rides its own boundary: a sort that reordered one and
    # not the other would pass the assertion above.
    assert [a.amount for a in emitted] == [Decimal("-3"), Decimal("-2"), Decimal("-1")]


def test_payments_settling_on_one_boundary_keep_the_venue_s_delivery_order() -> None:
    """Every symbol settles on the same hourly boundary, so same-`time` records
    are the common case rather than a corner.

    The sort is stable, which leaves their relative order the venue's. There is
    nothing better to order them by — they are distinct payments on distinct
    symbols, keyed apart by `symbol`, so the watermark treats them as one
    boundary either way and any reshuffle would be this module inventing a
    sequence the venue did not report.
    """
    same_boundary = [
        funding(time_ms=1681222254710, coin="SOL", usdc="-1"),
        funding(time_ms=1681222254710, coin="ETH", usdc="-2"),
        funding(time_ms=1681222254710, coin="BTC", usdc="-3"),
    ]

    emitted = accruals(same_boundary, account_id="hyperliquid-mainnet-0xabc", ts_init_ns=42)

    assert [a.symbol for a in emitted] == ["SOL", "ETH", "BTC"]
