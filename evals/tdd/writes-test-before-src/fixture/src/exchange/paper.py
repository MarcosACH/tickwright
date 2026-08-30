from decimal import Decimal


class FeeModel:
    """Charges a flat fee on every fill, maker or taker."""

    def __init__(self, flat_fee: Decimal) -> None:
        self._flat_fee = flat_fee

    def fee_for_fill(self, *, is_maker: bool, notional: Decimal) -> Decimal:
        """Return the fee owed for a fill of the given notional."""
        return self._flat_fee
