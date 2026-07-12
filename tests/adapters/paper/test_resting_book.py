"""``RestingBook`` — the paper venue's resting LIMITs and their working
remainders (ADR-0012). Extracted from ``PaperExchange`` so the one invariant
that ties an order to its remainder — cap each fill to what is still working,
converge to exactly the order size, then lift the order off the book — lives in
one place and is unit-testable without driving a whole tick stream.
"""

from decimal import Decimal

import pytest

from tickwright.adapters.paper.book import RestingBook
from tickwright.domain import (
    InvariantViolation,
    OrderType,
    PlaceOrder,
    Side,
    TimeInForce,
)


def _limit(qty: str = "1", *, cloid: str = "0xabc", symbol: str = "BTC") -> PlaceOrder:
    return PlaceOrder(
        cloid=cloid,
        symbol=symbol,
        side=Side.BUY,
        quantity=Decimal(qty),
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        price=Decimal("41000"),
    )


def test_a_rested_order_appears_in_the_resting_snapshot() -> None:
    book = RestingBook()
    order = _limit()

    book.rest(order)

    assert book.resting() == [order]


def test_apply_fill_caps_to_the_working_remainder_and_reports_incomplete() -> None:
    # The model may offer more than remains; the book never over-fills.
    book = RestingBook()
    book.rest(_limit(qty="1"))

    filled, complete = book.apply_fill("0xabc", Decimal("0.4"))

    assert filled == Decimal("0.4")
    assert complete is False
    assert book.resting() != []  # still working, still on the book


def test_a_fill_offering_more_than_remains_is_capped_to_the_remainder() -> None:
    book = RestingBook()
    book.rest(_limit(qty="1"))
    book.apply_fill("0xabc", Decimal("0.8"))  # remainder now 0.2

    filled, complete = book.apply_fill("0xabc", Decimal("0.5"))  # model offers 0.5

    assert filled == Decimal("0.2")  # only the remainder fills
    assert complete is True


def test_apply_fill_that_exhausts_the_remainder_completes_and_lifts_the_order() -> None:
    book = RestingBook()
    book.rest(_limit(qty="1"))

    filled, complete = book.apply_fill("0xabc", Decimal("1"))

    assert filled == Decimal("1")
    assert complete is True
    assert book.resting() == []  # lifted off the book on completion


def test_partial_fills_converge_to_exactly_the_order_size() -> None:
    book = RestingBook()
    book.rest(_limit(qty="1"))

    filled = [book.apply_fill("0xabc", Decimal("0.4")) for _ in range(3)]

    assert [qty for qty, _ in filled] == [Decimal("0.4"), Decimal("0.4"), Decimal("0.2")]
    assert [complete for _, complete in filled] == [False, False, True]
    assert sum((qty for qty, _ in filled), Decimal(0)) == Decimal("1")


def test_has_partial_is_false_until_a_fill_reduces_the_remainder() -> None:
    # This is what the venue reads to announce LIVE only for an untouched order.
    book = RestingBook()
    book.rest(_limit(qty="1"))
    assert book.has_partial("0xabc") is False

    book.apply_fill("0xabc", Decimal("0.4"))
    assert book.has_partial("0xabc") is True


def test_remove_returns_the_order_then_none_once_gone() -> None:
    book = RestingBook()
    order = _limit()
    book.rest(order)

    assert book.remove("0xabc") is order  # lifted off, returned to the caller
    assert book.remove("0xabc") is None  # nothing rests under it now
    assert book.resting() == []


def test_remove_of_an_unknown_cloid_is_a_benign_none() -> None:
    assert RestingBook().remove("0xnope") is None


def test_applying_a_fill_to_an_unrested_order_is_an_invariant_violation() -> None:
    # The book's contract: fills only ever land on a resting order.
    with pytest.raises(InvariantViolation):
        RestingBook().apply_fill("0xghost", Decimal("1"))
