"""``PortfolioProjection`` and the scoped ``Portfolio`` facade it hands out.

The projection is exercised through its write verb (``apply_fill`` — the
fill-apply path's single entry) and read back through the ``domain`` seam a
strategy actually holds, never through its internals.
"""

from decimal import Decimal

import pytest

from tickwright.domain import (
    Account,
    AccountSpec,
    OrderFilled,
    OrderFillEvent,
    Portfolio,
    Side,
)
from tickwright.engine.portfolio import PortfolioProjection

_SPEC = AccountSpec(account_id="paper-default", genesis_collateral=Decimal("100000"))


def _fill(
    *,
    trade_id: str,
    quantity: str,
    price: str,
    symbol: str = "BTC",
    strategy_id: str = "alpha",
) -> OrderFillEvent:
    return OrderFilled(
        ts_event=1_000,
        ts_init=1_000,
        cloid=f"0x{strategy_id}-{trade_id}",
        strategy_id=strategy_id,
        signal_id=f"{strategy_id}:{symbol}:1",
        symbol=symbol,
        trade_id=trade_id,
        quantity=Decimal(quantity),
        price=Decimal(price),
        cum_qty=Decimal(quantity),
    )


def _projection(genesis: str = "100000") -> PortfolioProjection:
    return PortfolioProjection(
        account=Account.open(_SPEC, genesis_collateral=Decimal(genesis), ts_ns=7)
    )


def test_a_fill_lands_in_its_partition_and_reads_back_through_the_seam() -> None:
    projection = _projection()
    portfolio: Portfolio = projection.for_strategy("alpha")

    projection.apply_fill(_fill(trade_id="f1", quantity="2", price="100"), side=Side.BUY)

    view = portfolio.position("BTC")
    assert view is not None
    assert view.symbol == "BTC"
    assert view.size == Decimal("2")
    assert view.entry_price == Decimal("100")
    assert view.realized_pnl == Decimal("0")


def test_a_never_traded_symbol_reads_none() -> None:
    portfolio = _projection().for_strategy("alpha")

    assert portfolio.position("BTC") is None
    assert portfolio.open_positions() == ()


def test_a_symbol_traded_flat_keeps_its_realized_and_leaves_the_open_set() -> None:
    projection = _projection()
    portfolio = projection.for_strategy("alpha")
    projection.apply_fill(_fill(trade_id="f1", quantity="2", price="100"), side=Side.BUY)

    projection.apply_fill(_fill(trade_id="f2", quantity="2", price="150"), side=Side.SELL)

    view = portfolio.position("BTC")
    assert view is not None
    assert view.size == Decimal("0")
    assert view.entry_price == Decimal("0")
    assert view.realized_pnl == Decimal("100")  # 2 x (150 - 100), worked by hand
    # "Flat with history" stays honestly distinct from "never here" (ADR-0041 §3).
    assert portfolio.open_positions() == ()


def test_open_positions_lists_only_the_non_flat_partitions() -> None:
    projection = _projection()
    portfolio = projection.for_strategy("alpha")
    projection.apply_fill(_fill(trade_id="f1", quantity="2", price="100"), side=Side.BUY)
    projection.apply_fill(
        _fill(trade_id="f2", quantity="1", price="3000", symbol="ETH"), side=Side.BUY
    )
    projection.apply_fill(
        _fill(trade_id="f3", quantity="1", price="3100", symbol="ETH"), side=Side.SELL
    )

    assert [view.symbol for view in portfolio.open_positions()] == ["BTC"]


def test_realized_pnl_accrues_to_the_account_cash_line() -> None:
    projection = _projection("100000")
    portfolio = projection.for_strategy("alpha")

    projection.apply_fill(_fill(trade_id="f1", quantity="2", price="100"), side=Side.BUY)
    assert portfolio.account().cash == Decimal("100000")  # opening a leg realizes nothing

    projection.apply_fill(_fill(trade_id="f2", quantity="2", price="80"), side=Side.SELL)
    assert portfolio.account().cash == Decimal("99960")  # 2 x (80 - 100) = -40


def test_a_strategy_reads_only_its_own_partition() -> None:
    projection = _projection()
    projection.apply_fill(
        _fill(trade_id="f1", quantity="2", price="100", strategy_id="alpha"), side=Side.BUY
    )
    projection.apply_fill(
        _fill(trade_id="f1", quantity="5", price="100", strategy_id="beta"), side=Side.BUY
    )

    assert projection.for_strategy("alpha").position("BTC").size == Decimal("2")  # type: ignore[union-attr]
    assert projection.for_strategy("beta").position("BTC").size == Decimal("5")  # type: ignore[union-attr]


def test_a_redelivered_fill_moves_neither_the_position_nor_the_cash_line() -> None:
    projection = _projection("100000")
    portfolio = projection.for_strategy("alpha")
    projection.apply_fill(_fill(trade_id="f1", quantity="2", price="100"), side=Side.BUY)
    fill = _fill(trade_id="f2", quantity="2", price="150")
    projection.apply_fill(fill, side=Side.SELL)

    projection.apply_fill(fill, side=Side.SELL)

    assert portfolio.position("BTC").realized_pnl == Decimal("100")  # type: ignore[union-attr]
    assert portfolio.account().cash == Decimal("100100")


@pytest.mark.parametrize("symbol", ["BTC", "ETH"])
def test_every_tier_one_field_reads_a_number(symbol: str) -> None:
    projection = _projection()
    projection.apply_fill(
        _fill(trade_id="f1", quantity="2", price="100", symbol=symbol), side=Side.BUY
    )

    view = projection.for_strategy("alpha").position(symbol)
    assert view is not None
    assert all(
        isinstance(value, Decimal)
        for value in (view.size, view.entry_price, view.realized_pnl, view.fees, view.funding)
    )
