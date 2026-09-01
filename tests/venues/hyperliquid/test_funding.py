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
