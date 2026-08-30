from decimal import Decimal

from exchange.paper import FeeModel


def test_taker_fill_pays_the_flat_fee() -> None:
    model = FeeModel(flat_fee=Decimal("1.50"))

    fee = model.fee_for_fill(is_maker=False, notional=Decimal("10000"))

    assert fee == Decimal("1.50")
