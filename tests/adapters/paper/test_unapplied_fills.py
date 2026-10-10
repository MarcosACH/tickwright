"""``UnappliedFills``: the paper venue's own fills the store has not applied yet
(ADR-0057). The venue adds them to the store net, so two quick reduce-only
orders cannot both close the same position.
"""

from decimal import Decimal

from tickwright.adapters.paper.unapplied import UnappliedFills
from tickwright.domain import FillReport, Side


def _fill(quantity: str, *, trade_id: str, symbol: str = "BTC") -> FillReport:
    return FillReport(
        ts_event=1_000,
        ts_init=1_000,
        cloid="0xabc",
        symbol=symbol,
        trade_id=trade_id,
        quantity=Decimal(quantity),
        price=Decimal("50000"),
    )


def test_an_unapplied_fill_counts_with_the_sign_of_its_side() -> None:
    fills = UnappliedFills(applied=lambda cloid: ())
    fills.record(_fill("2", trade_id="1"), Side.BUY)
    fills.record(_fill("0.5", trade_id="2"), Side.SELL)
    fills.record(_fill("1", trade_id="3", symbol="ETH"), Side.BUY)

    assert fills.signed_size("BTC") == Decimal("1.5")
    assert fills.signed_size("ETH") == Decimal("1")
    assert fills.signed_size("SOL") == Decimal("0")


def test_a_fill_stops_counting_once_the_store_applies_it() -> None:
    # Counting it after that would count it twice: once in the store net and
    # once here.
    applied: set[str] = set()
    fills = UnappliedFills(applied=lambda cloid: applied)
    fill = _fill("2", trade_id="1")
    fills.record(fill, Side.BUY)
    assert fills.signed_size("BTC") == Decimal("2")

    applied.add(fill.event_id)

    assert fills.signed_size("BTC") == Decimal("0")


def test_an_applied_fill_is_dropped_so_the_store_is_never_asked_about_it_again() -> None:
    # Dropped, not skipped: the list must not grow with every fill of a run.
    asked: list[str] = []
    applied: set[str] = set()

    def applied_fills(cloid: str) -> set[str]:
        asked.append(cloid)
        return applied

    fills = UnappliedFills(applied=applied_fills)
    fill = _fill("2", trade_id="1")
    fills.record(fill, Side.BUY)
    applied.add(fill.event_id)
    fills.signed_size("BTC")
    asked.clear()

    fills.signed_size("BTC")

    assert asked == []
