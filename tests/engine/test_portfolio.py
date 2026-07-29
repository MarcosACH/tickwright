"""``PortfolioProjection`` and the scoped ``Portfolio`` facade it hands out.

The projection is exercised through its write verb (``apply_fill`` — the
fill-apply path's single entry) and read back through the ``domain`` seam a
strategy actually holds, never through its internals.
"""

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

import pytest
from ledgers import book_fill

from tickwright.adapters.clock import ManualClock
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    Account,
    AccountSpec,
    InvariantViolation,
    OrderFilled,
    OrderFillEvent,
    Portfolio,
    Position,
    Side,
    Store,
)
from tickwright.engine.portfolio import PortfolioProjection
from tickwright.observability.testing import capture_events


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


def _projection(
    genesis: str | None = "100000", *, store: Store | None = None
) -> PortfolioProjection:
    """A ledger on a paper-shaped account, unless ``genesis`` is ``None`` — which
    is the *live* shape, where the opening value is ingested from the venue
    rather than declared (ADR-0042 §6) and is the predicate recovery reads."""
    spec = AccountSpec(
        account_id="paper-default",
        genesis_collateral=Decimal(genesis) if genesis is not None else None,
    )
    return PortfolioProjection(
        spec=spec,
        store=store if store is not None else SQLiteStore(":memory:"),
        clock=ManualClock(7),
    )


def test_a_fill_lands_in_its_partition_and_reads_back_through_the_seam() -> None:
    projection = _projection()
    portfolio: Portfolio = projection.for_strategy("alpha")

    book_fill(projection, _fill(trade_id="f1", quantity="2", price="100"), side=Side.BUY)

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
    book_fill(projection, _fill(trade_id="f1", quantity="2", price="100"), side=Side.BUY)

    book_fill(projection, _fill(trade_id="f2", quantity="2", price="150"), side=Side.SELL)

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
    book_fill(projection, _fill(trade_id="f1", quantity="2", price="100"), side=Side.BUY)
    book_fill(
        projection, _fill(trade_id="f2", quantity="1", price="3000", symbol="ETH"), side=Side.BUY
    )
    book_fill(
        projection, _fill(trade_id="f3", quantity="1", price="3100", symbol="ETH"), side=Side.SELL
    )

    assert [view.symbol for view in portfolio.open_positions()] == ["BTC"]


def test_realized_pnl_accrues_to_the_account_cash_line() -> None:
    projection = _projection("100000")
    portfolio = projection.for_strategy("alpha")

    book_fill(projection, _fill(trade_id="f1", quantity="2", price="100"), side=Side.BUY)
    assert portfolio.account().cash == Decimal("100000")  # opening a leg realizes nothing

    book_fill(projection, _fill(trade_id="f2", quantity="2", price="80"), side=Side.SELL)
    assert portfolio.account().cash == Decimal("99960")  # 2 x (80 - 100) = -40


def test_a_strategy_reads_only_its_own_partition() -> None:
    projection = _projection()
    book_fill(
        projection,
        _fill(trade_id="f1", quantity="2", price="100", strategy_id="alpha"),
        side=Side.BUY,
    )
    book_fill(
        projection,
        _fill(trade_id="f1", quantity="5", price="100", strategy_id="beta"),
        side=Side.BUY,
    )

    assert projection.for_strategy("alpha").position("BTC").size == Decimal("2")  # type: ignore[union-attr]
    assert projection.for_strategy("beta").position("BTC").size == Decimal("5")  # type: ignore[union-attr]


def test_a_redelivered_fill_moves_neither_the_position_nor_the_cash_line() -> None:
    """The at-least-once guarantee (ADR-0025) for *both* ledgers, asserted here
    because here is where it is produced: ``Position.apply`` is the fill's one
    gatekeeper, and ``Account`` keeps no applied set of its own to fall back on.

    The redelivered leg is a **partial** reduce, deliberately. Closing the whole
    position instead would leave the redelivery landing on a flat record, where
    it opens a fresh short rather than realizing again — realized PnL and cash
    both survive by accident and the assertions pass with the gatekeeper gone.
    Reducing 4 to 2 makes a second application genuinely double-count, so
    deleting ``Position``'s dedup fails this test.
    """
    projection = _projection("100000")
    portfolio = projection.for_strategy("alpha")
    book_fill(projection, _fill(trade_id="f1", quantity="4", price="100"), side=Side.BUY)
    fill = _fill(trade_id="f2", quantity="2", price="150")
    book_fill(projection, fill, side=Side.SELL)

    book_fill(projection, fill, side=Side.SELL)

    view = portfolio.position("BTC")
    assert view is not None
    assert view.size == Decimal("2")  # not 0 — the reduce was applied once
    assert view.realized_pnl == Decimal("100")  # 2 x (150 - 100), booked once
    assert portfolio.account().cash == Decimal("100100")


@pytest.mark.parametrize("symbol", ["BTC", "ETH"])
def test_every_tier_one_field_reads_a_number(symbol: str) -> None:
    projection = _projection()
    book_fill(
        projection, _fill(trade_id="f1", quantity="2", price="100", symbol=symbol), side=Side.BUY
    )

    view = projection.for_strategy("alpha").position(symbol)
    assert view is not None
    assert all(
        isinstance(value, Decimal)
        for value in (view.size, view.entry_price, view.realized_pnl, view.fees, view.funding)
    )


def _announcements(logs: Sequence[Mapping[str, Any]]) -> list[tuple[str, str]]:
    """Each position announcement as ``(name, size)``, in order — the only place
    a position change is observable from outside (ADR-0045 §1), so the payload
    is as much of the contract as the name is."""
    return [
        (str(log["event"]), str(log["size"]))
        for log in logs
        if str(log["event"]).startswith("position.")
    ]


def test_a_fill_that_flips_through_zero_announces_closed_then_opened() -> None:
    """The residual opens a fresh average-cost record, so a flip is genuinely two
    facts and is announced as two — in that order (ADR-0045 §2). A position
    change is never a bus event, so this catalog is the only place it shows.

    Each half carries the size *it* produced: the old leg closed at zero, and
    only then did the residual open at -3. Announcing the residual on both would
    report a close at a non-zero size and invert the name.
    """
    projection = _projection()
    book_fill(projection, _fill(trade_id="f1", quantity="2", price="100"), side=Side.BUY)

    with capture_events() as logs:
        book_fill(projection, _fill(trade_id="f2", quantity="5", price="120"), side=Side.SELL)

    assert _announcements(logs) == [("position.closed", "0"), ("position.opened", "-3")]
    view = projection.for_strategy("alpha").position("BTC")
    assert view is not None
    assert view.size == Decimal("-3")
    assert view.entry_price == Decimal("120")  # the residual, not a blended entry
    assert view.realized_pnl == Decimal("40")  # the whole closed long leg


def test_each_regime_announces_its_own_name_and_the_size_it_produced() -> None:
    projection = _projection()

    with capture_events() as logs:
        book_fill(projection, _fill(trade_id="f1", quantity="2", price="100"), side=Side.BUY)
        book_fill(projection, _fill(trade_id="f2", quantity="1", price="120"), side=Side.BUY)
        book_fill(projection, _fill(trade_id="f3", quantity="3", price="130"), side=Side.SELL)

    assert _announcements(logs) == [
        ("position.opened", "2"),
        ("position.changed", "3"),
        ("position.closed", "0"),
    ]


def test_a_redelivered_fill_announces_nothing() -> None:
    projection = _projection()
    fill = _fill(trade_id="f1", quantity="2", price="100")
    book_fill(projection, fill, side=Side.BUY)

    with capture_events() as logs:
        book_fill(projection, fill, side=Side.BUY)

    assert _announcements(logs) == []


def test_a_zero_quantity_fill_faults_the_projection_and_announces_nothing() -> None:
    """The aggregate's refusal is the projection's too: an ``InvariantViolation``
    pierces containment and faults the engine rather than being absorbed
    (ADR-0014). Without it the projection would announce ``position.opened``
    with ``size="0"`` — the payload shape reserved for a *close* — so an observer
    would read an open at zero size, which the catalog cannot name (ADR-0045 §2).
    """
    projection = _projection()
    portfolio: Portfolio = projection.for_strategy("alpha")

    with (
        capture_events() as logs,
        pytest.raises(InvariantViolation, match="non-positive quantity"),
    ):
        book_fill(projection, _fill(trade_id="f1", quantity="0", price="100"), side=Side.BUY)

    assert _announcements(logs) == []
    assert portfolio.position("BTC") is None  # not even a materialized empty partition
    assert portfolio.account().cash == Decimal("100000")


def test_recover_restores_the_cash_line_and_every_partition_it_finds() -> None:
    """Cumulative realized PnL, the fee and funding lines and per-strategy
    attribution are reconstructible from nowhere else (ADR-0043 §1), so a restart
    that does not read them back has lost them.

    The durable rows are declared here rather than produced by a first life's
    fills: what a fill computes is asserted by the cases above, and what makes it
    durable is asserted at the ``ExecutionManager`` seam. Restoring *these*
    numbers is this case's subject, so they are literals — a first life would
    hand recovery the same arithmetic the assertions would then have to repeat.
    """
    store = SQLiteStore(":memory:")
    store.checkpoint_ledger(
        account=Account.restore(
            account_id="paper-default",
            genesis_collateral=Decimal("100000"),
            genesis_ts_ns=7,
            cash=Decimal("100100"),
        ),
        positions=[
            Position(
                strategy_id="alpha",
                symbol="BTC",
                signed_size=Decimal("2"),
                entry_price=Decimal("100"),
                realized_pnl=Decimal("100"),
            ),
            Position(
                strategy_id="beta",
                symbol="ETH",
                signed_size=Decimal("-5"),
                entry_price=Decimal("3000"),
            ),
        ],
        ts_ns=2_000,
    )
    projection = _projection(store=store)

    projection.recover()

    alpha = projection.for_strategy("alpha").position("BTC")
    assert alpha is not None
    assert alpha.size == Decimal("2")
    assert alpha.realized_pnl == Decimal("100")
    beta = projection.for_strategy("beta").position("ETH")
    assert beta is not None
    assert beta.size == Decimal("-5")
    # One pool per process, so either facade reads the same restored line — and
    # it is the accrued cash, not the genesis the projection opened at.
    assert projection.for_strategy("alpha").account().cash == Decimal("100100")


def test_recover_fabricates_no_partition_the_store_does_not_hold() -> None:
    """A restart may not invent a flat position (ADR-0041 §3/§6). "Flat with
    history" and "never traded here" are different answers, and recovery has to
    keep telling them apart: the first is a row the store holds, the second is a
    row it does not, and a projection that seeded partitions for the symbols it
    expects would report the second as the first — a traded-flat record for a
    symbol nothing ever filled on.
    """
    store = SQLiteStore(":memory:")
    store.checkpoint_ledger(
        account=Account.restore(
            account_id="paper-default",
            genesis_collateral=Decimal("100000"),
            genesis_ts_ns=7,
            cash=Decimal("100100"),
        ),
        positions=[Position(strategy_id="alpha", symbol="BTC", realized_pnl=Decimal("100"))],
        ts_ns=2_000,
    )
    projection = _projection(store=store)

    projection.recover()

    portfolio = projection.for_strategy("alpha")
    assert portfolio.position("ETH") is None  # no row: honestly absent
    restored = portfolio.position("BTC")
    assert restored is not None  # a row: flat, and its realized survives
    assert restored.size == Decimal("0")
    assert restored.realized_pnl == Decimal("100")
    assert portfolio.open_positions() == ()  # flat holds no exposure to list


def test_a_paper_start_against_an_empty_store_seeds_the_configured_genesis() -> None:
    """The paper genesis row is written inside ``recover()``, not at the first fill
    and not on a cadence (ADR-0043 §6).

    Its opening cash is a config value already in hand, so the write cannot fail
    on connectivity and has nothing to retry — which is why it sits here rather
    than at the barrier where live's ingested twin has to. Writing it later would
    reproduce on the *default* path the state the barrier's live step exists to
    prevent: strategies started, ``account()`` read, and no cash at all.
    """
    store = SQLiteStore(":memory:")
    projection = _projection("50000", store=store)

    projection.recover()

    seeded = store.load_account()
    assert seeded is not None
    assert seeded.account_id == "paper-default"
    assert seeded.genesis_collateral == Decimal("50000")
    assert seeded.cash == Decimal("50000")  # nothing has accrued away from it yet


def test_a_restart_restores_the_ledger_rather_than_re_seeding_genesis() -> None:
    """Seeded exactly once. The second life opens its in-memory account at the
    same configured genesis, so a ``recover()`` that wrote unconditionally would
    look correct on a first run and silently reset the cash line on every restart
    after one — paper's ledger has no venue to be corrected against.
    """
    store = SQLiteStore(":memory:")
    _projection("50000", store=store).recover()
    # The first life then trades at a loss and checkpoints, as a fill would.
    store.checkpoint_ledger(
        account=Account.restore(
            account_id="paper-default",
            genesis_collateral=Decimal("50000"),
            genesis_ts_ns=7,
            cash=Decimal("49000"),
        ),
        ts_ns=3_000,
    )

    second = _projection("50000", store=store)
    second.recover()

    assert second.account().cash == Decimal("49000")  # restored, not back at genesis
    reread = store.load_account()
    assert reread is not None
    assert reread.cash == Decimal("49000")


def test_a_live_start_against_an_empty_store_seeds_nothing() -> None:
    """The seed is paper's alone, gated on the genesis the venue declares
    (ADR-0043 §10). Live's opening value is ``accountValue − Σ unrealized_pnl``,
    ingested at the startup barrier that has not run yet — so a seed here would
    persist a zero genesis as though an operator had chosen it, and #191's
    materialisation would be correcting a row instead of creating one.
    """
    store = SQLiteStore(":memory:")

    _projection(None, store=store).recover()

    assert store.load_account() is None
