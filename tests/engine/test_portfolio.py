"""``PortfolioProjection`` and the scoped ``Portfolio`` facade it hands out.

The projection is exercised through its write verb (``apply_fill`` — the
fill-apply path's single entry) and read back through the ``domain`` seam a
strategy actually holds, never through its internals.
"""

from collections.abc import Mapping, Sequence
from dataclasses import fields
from decimal import Decimal
from typing import Any

import pytest
from ledgers import book_fill
from venue_doubles import account_state

from tickwright.adapters.clock import ManualClock
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    Account,
    AccountSpec,
    FundingAccrual,
    InvariantViolation,
    MarkTick,
    Order,
    OrderFilled,
    OrderFillEvent,
    OrderState,
    Portfolio,
    Position,
    PositionView,
    Side,
    Store,
    StoreAccountMismatch,
)
from tickwright.domain.enums import OrderType
from tickwright.engine.portfolio import PortfolioProjection
from tickwright.observability.testing import capture_events


def _fill(
    *,
    trade_id: str,
    quantity: str,
    price: str,
    symbol: str = "BTC",
    strategy_id: str = "alpha",
    fee: str = "0",
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
        fee=Decimal(fee),
    )


def _mark(*, price: str, ts_event: int, symbol: str = "BTC") -> MarkTick:
    """One symbol's mark as a feed published it — the Tier-2 valuation input.

    Provenance-free by construction (ADR-0039): this is the same shape the live
    ``activeAssetCtx`` mark and replay's last-trade proxy both arrive in, which
    is what lets the projection hold no ``if live:`` for the mark.
    """
    return MarkTick(ts_event=ts_event, ts_init=ts_event, symbol=symbol, price=Decimal(price))


def _view_fields() -> list[str]:
    """Every field the ``Portfolio`` seam's position snapshot exposes."""
    return [field.name for field in fields(PositionView)]


_HOUR = 3_600_000_000_000
"""One epoch-aligned funding boundary, and the unit the cases below step in."""


def _accrual(
    *, amount: str, boundary: int, symbol: str = "BTC", account_id: str = "paper-default"
) -> FundingAccrual:
    """One boundary's settled funding, as an ``Exchange`` adapter produced it.

    Signed as the venue reports it (ADR-0037): ``< 0`` paid, ``> 0`` received.
    The projection never recomputes the amount — paper generated it and live
    ingests it verbatim, so what arrives here is already the payment.
    """
    return FundingAccrual(
        ts_event=boundary,
        ts_init=boundary,
        account_id=account_id,
        symbol=symbol,
        boundary_ts_ns=boundary,
        amount=Decimal(amount),
    )


def _projection(
    genesis: str | None = "100000", *, store: Store | None = None
) -> PortfolioProjection:
    """A ledger on a paper-shaped account, unless ``genesis`` is ``None`` — which
    is the *live* shape, where the opening value is ingested from the venue
    rather than declared (ADR-0042 §6) and is the predicate recovery reads."""
    spec = AccountSpec(
        # Two segments on paper against live's three (ADR-0038/0042 §5), so a
        # row written by one shape is never confusable with the other's.
        account_id="paper-default" if genesis is not None else "hyperliquid-testnet-0xabc",
        genesis_collateral=Decimal(genesis) if genesis is not None else None,
    )
    return PortfolioProjection(
        spec=spec,
        store=store if store is not None else SQLiteStore(":memory:"),
        clock=ManualClock(7),
    )


def _record_order_history(store: Store) -> None:
    """Leave the store holding saga history and nothing else — the shape a
    pre-ledger database upgrades in (ADR-0043 §8).

    One order is enough: the refusal turns on ``has_orders()``, an existence
    question the startup check is entitled to ask *instead of* the mass read.
    """
    store.checkpoint(
        Order(
            cloid="0xalpha-1",
            strategy_id="alpha",
            signal_id="alpha:BTC:1",
            symbol="BTC",
            side=Side.BUY,
            quantity=Decimal("0.5"),
            order_type=OrderType.MARKET,
            state=OrderState.SUBMITTED,
        ),
        ts_ns=1_000,
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


def test_an_observed_mark_surfaces_its_instant_and_never_its_value() -> None:
    """The mark reaches the seam as **freshness only** (ADR-0039, ADR-0041 §6).

    A strategy holds a ``Clock``, so ``mark_ts`` is all it needs to judge a
    stale-but-present mark for itself — and there is no max-age on the read path
    to judge it for them. The mark **value** stays off the view deliberately: it
    is an accounting input, not a strategy signal, and exposing it would make it
    one without any of the callbacks ADR-0027 reserves for that.
    """
    projection = _projection()
    portfolio: Portfolio = projection.for_strategy("alpha")
    book_fill(projection, _fill(trade_id="f1", quantity="2", price="100"), side=Side.BUY)

    projection.observe_mark(_mark(price="110", ts_event=9_000))

    view = portfolio.position("BTC")
    assert view is not None
    assert view.mark_ts == 9_000
    # Freshness is the *only* thing the mark contributes to the view's own field
    # set: nothing here carries 110.
    assert [name for name in _view_fields() if "mark" in name] == ["mark_ts"]
    assert Decimal("110") not in [getattr(view, name) for name in _view_fields()]


def test_a_long_marked_above_its_entry_reads_an_unrealized_profit() -> None:
    """``signed_size × (mark − entry)`` — 2 × (110 − 100) = +20, worked by hand.

    Tier-2 and therefore **recomputed on read** rather than accumulated: nothing
    booked it, and nothing has to un-book it when the mark moves back.
    """
    projection = _projection()
    portfolio = projection.for_strategy("alpha")
    book_fill(projection, _fill(trade_id="f1", quantity="2", price="100"), side=Side.BUY)

    projection.observe_mark(_mark(price="110", ts_event=9_000))

    view = portfolio.position("BTC")
    assert view is not None
    assert view.unrealized_pnl == Decimal("20")
    # Tier-1 is untouched by the mark: nothing was closed, so nothing realized.
    assert view.realized_pnl == Decimal("0")


def test_a_short_marked_above_its_entry_reads_an_unrealized_loss() -> None:
    """The sign rides the *position*, not the mark's direction: −2 × (110 − 100)
    = −20. A short losing as the mark rises is the case a magnitude-only
    formula gets backwards, and it costs an account its margin silently."""
    projection = _projection()
    portfolio = projection.for_strategy("alpha")
    book_fill(projection, _fill(trade_id="f1", quantity="2", price="100"), side=Side.SELL)

    projection.observe_mark(_mark(price="110", ts_event=9_000))

    view = portfolio.position("BTC")
    assert view is not None
    assert view.size == Decimal("-2")
    assert view.unrealized_pnl == Decimal("-20")


def test_notional_is_the_whole_accounts_exposure_not_the_strategys_own_slice() -> None:
    """The two grains one view carries, and the case that tells them apart.

    ``unrealized_pnl`` is the **own-attribution slice** — a number a fill
    partitions linearly. ``notional`` is **position-grain**: the venue holds one
    position per symbol, keyed per position and never per strategy, so it is
    computed off the symbol's *account-net* size summed over every partition
    (ADR-0041 §4). Two strategies long the same symbol therefore read one
    notional and two different unrealized PnLs.

    Alpha is long 2 and beta long 3, so the account nets +5 and the mark is 110:
    notional 550, against the 220 alpha's own slice would have given.
    """
    projection = _projection()
    book_fill(projection, _fill(trade_id="f1", quantity="2", price="100"), side=Side.BUY)
    book_fill(
        projection,
        _fill(trade_id="f2", quantity="3", price="100", strategy_id="beta"),
        side=Side.BUY,
    )

    projection.observe_mark(_mark(price="110", ts_event=9_000))

    alpha = projection.for_strategy("alpha").position("BTC")
    beta = projection.for_strategy("beta").position("BTC")
    assert alpha is not None and beta is not None
    assert alpha.notional == beta.notional == Decimal("550")
    assert alpha.unrealized_pnl == Decimal("20")  # 2 x (110 - 100)
    assert beta.unrealized_pnl == Decimal("30")  # 3 x (110 - 100)


def test_notional_is_a_magnitude_so_a_short_account_still_reads_positive() -> None:
    """Exposure has no direction — ``|account net| × mark``. A negative notional
    would flip the sign of every margin figure computed off it downstream."""
    projection = _projection()
    portfolio = projection.for_strategy("alpha")
    book_fill(projection, _fill(trade_id="f1", quantity="2", price="100"), side=Side.SELL)

    projection.observe_mark(_mark(price="110", ts_event=9_000))

    view = portfolio.position("BTC")
    assert view is not None
    assert view.notional == Decimal("220")


def test_account_equity_is_cash_plus_the_unrealized_pnl_of_every_partition() -> None:
    """``equity = cash + Σ uPnL`` over the **whole** account (ADR-0041 §4).

    Not the reading strategy's share of it: collateral is one pool per process
    and every partition's open exposure backs the same bucket, so a strategy
    reading equity reads the account's, unscoped. Alpha is long 2 BTC from 100
    and beta short 1 ETH from 3000; marked at 110 and 2900 that is +20 and +100
    against a 100 000 opening cash line — 100 120, worked by hand.
    """
    projection = _projection("100000")
    book_fill(projection, _fill(trade_id="f1", quantity="2", price="100"), side=Side.BUY)
    book_fill(
        projection,
        _fill(trade_id="f2", quantity="1", price="3000", symbol="ETH", strategy_id="beta"),
        side=Side.SELL,
    )

    projection.observe_mark(_mark(price="110", ts_event=9_000))
    projection.observe_mark(_mark(price="2900", ts_event=9_000, symbol="ETH"))

    account = projection.for_strategy("alpha").account()
    assert account.cash == Decimal("100000")  # Tier-1, untouched by any mark
    assert account.equity == Decimal("100120")


def test_one_unmarked_symbol_makes_the_whole_account_equity_unknown() -> None:
    """The cross-strategy coupling ADR-0041 §6 names, and it is not a defect.

    Account equity sums over every position, so one symbol with no mark makes
    the total genuinely uncomputable — for *every* strategy, including one whose
    own symbols are all marked. Reporting the marked subset's total instead
    would be a number that looks like equity and is not.

    ``cash`` is exempt outright: it is Tier-1, so a cold start is not a
    reporting blackout.
    """
    projection = _projection("100000")
    book_fill(projection, _fill(trade_id="f1", quantity="2", price="100"), side=Side.BUY)
    book_fill(
        projection,
        _fill(trade_id="f2", quantity="1", price="3000", symbol="ETH", strategy_id="beta"),
        side=Side.SELL,
    )

    projection.observe_mark(_mark(price="110", ts_event=9_000))

    account = projection.for_strategy("alpha").account()
    assert account.equity is None
    assert account.cash == Decimal("100000")


def test_an_account_holding_nothing_reads_its_equity_as_its_cash() -> None:
    """Per-term again: with no open exposure the Σ has no mark-dependent term to
    wait on, so equity is the cash line rather than ``None`` (ADR-0041 §6)."""
    projection = _projection("100000")

    account = projection.for_strategy("alpha").account()
    assert account.equity == Decimal("100000")


def test_a_stale_mark_freezes_at_its_last_value_and_is_never_rejected_on_read() -> None:
    """Staleness is **exposed, not decided** (ADR-0039, ADR-0041 §6).

    The mark is ages older than the clock the projection holds, and the read
    still values against it and reports its instant. That is deliberate on both
    counts: there is no max-age on the read path, which keeps the ``Clock`` off
    it entirely, and a strategy — which has its own — compares ``mark_ts`` to now
    and decides for itself. Refusing here would turn every stale-mark read into
    the ``None`` that means *never valued*, collapsing two states a reader must
    be able to tell apart. The safety net for a dead mark stream is the reconcile
    cross-check, not a read-time age gate.
    """
    projection = _projection()
    portfolio = projection.for_strategy("alpha")
    book_fill(projection, _fill(trade_id="f1", quantity="2", price="100"), side=Side.BUY)
    # ``_projection`` runs on ``ManualClock(7)``; this mark predates even that.
    projection.observe_mark(_mark(price="110", ts_event=1))

    first = portfolio.position("BTC")
    second = portfolio.position("BTC")

    assert first is not None and second is not None
    assert first.unrealized_pnl == second.unrealized_pnl == Decimal("20")
    assert first.mark_ts == second.mark_ts == 1


def test_a_moved_mark_moves_the_valuation_on_the_very_next_read() -> None:
    """Nothing is memoized between reads (ADR-0034): the second read recomputes
    off the new mark rather than replaying the first read's answer.

    Two positions in one symbol, so the account-net half is exercised too — a
    cached notional would be just as wrong as a cached PnL, and the two are
    assembled from different inputs.
    """
    projection = _projection()
    portfolio = projection.for_strategy("alpha")
    book_fill(projection, _fill(trade_id="f1", quantity="2", price="100"), side=Side.BUY)

    projection.observe_mark(_mark(price="110", ts_event=9_000))
    before = portfolio.position("BTC")
    projection.observe_mark(_mark(price="120", ts_event=9_500))
    after = portfolio.position("BTC")

    assert before is not None and after is not None
    assert (before.unrealized_pnl, before.notional) == (Decimal("20"), Decimal("220"))
    assert (after.unrealized_pnl, after.notional) == (Decimal("40"), Decimal("240"))
    assert after.mark_ts == 9_500


def test_no_tier_two_value_reaches_the_durable_ledger() -> None:
    """Tier-2 is **never persisted** (ADR-0034, ADR-0043 §3): a restart recomputes
    a valuation, it does not recover one.

    The store here refuses to record anything the mark produced — it is asked
    what the ledger row holds after a marked fill, and the answer must be the
    Tier-1 lines alone. Persisting a valuation would make it heal-able state,
    which is the exact distinction the two-tier split rests on.
    """
    store = SQLiteStore(":memory:")
    projection = _projection("100000", store=store)
    # The atomic path in full this time, write included — the store is the
    # subject here, so the write that ``book_fill`` leaves out is the point.
    change = projection.apply_fill(_fill(trade_id="f1", quantity="2", price="100"), side=Side.BUY)
    store.checkpoint_ledger(account=change.account, positions=(change.position,), ts_ns=1_000)
    projection.project(change)

    projection.observe_mark(_mark(price="110", ts_event=9_000))

    # The mark moved the *view*; the durable row is untouched by it.
    view = projection.for_strategy("alpha").position("BTC")
    assert view is not None and view.unrealized_pnl == Decimal("20")
    stored = {position.symbol: position for position in store.all_positions()}["BTC"]
    assert stored.signed_size == Decimal("2")
    assert stored.entry_price == Decimal("100")
    account = store.load_account()
    assert account is not None
    assert account.cash == Decimal("100000")  # +20 of unrealized reached nothing
    # And a fresh projection over the same store reads no mark back at all.
    restarted = _projection("100000", store=store)
    restarted.recover()
    recovered = restarted.for_strategy("alpha").position("BTC")
    assert recovered is not None
    assert recovered.mark_ts is None
    assert recovered.unrealized_pnl is None


def test_a_position_the_projection_has_no_mark_for_reports_no_instant() -> None:
    """``mark_ts is None`` ⟺ the mark is absent — the one signal a reader has for
    telling "never valued" from "valued a while ago" (ADR-0041 §6)."""
    projection = _projection()
    portfolio = projection.for_strategy("alpha")
    book_fill(projection, _fill(trade_id="f1", quantity="2", price="100"), side=Side.BUY)

    view = portfolio.position("BTC")
    assert view is not None
    assert view.mark_ts is None


def test_a_mark_for_one_symbol_does_not_value_another() -> None:
    """The cache is keyed per symbol, so ETH's mark is not BTC's."""
    projection = _projection()
    portfolio = projection.for_strategy("alpha")
    book_fill(projection, _fill(trade_id="f1", quantity="2", price="100"), side=Side.BUY)

    projection.observe_mark(_mark(price="3000", ts_event=9_000, symbol="ETH"))

    view = portfolio.position("BTC")
    assert view is not None
    assert view.mark_ts is None


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


def test_a_fee_accrues_on_its_own_line_and_debits_cash_without_touching_pnl() -> None:
    """Three separate places a fee could have been smeared into, and is not.

    It is its own cumulative Tier-1 line; ``realized_pnl`` stays **gross** of it,
    as the venue keeps ``closedPnl`` separate; and ``entry_price`` is the price
    the fill traded at, not a cost basis loaded with charges (ADR-0036/0045 §3).
    Cash moves by the fee's **negative** — the second of its four accruing
    inputs — so a debited cost leaves less collateral than the run opened with.
    """
    projection = _projection("100000")
    portfolio = projection.for_strategy("alpha")

    book_fill(
        projection, _fill(trade_id="f1", quantity="2", price="100", fee="0.09"), side=Side.BUY
    )
    book_fill(
        projection, _fill(trade_id="f2", quantity="2", price="150", fee="0.135"), side=Side.SELL
    )

    view = portfolio.position("BTC")
    assert view is not None
    assert view.fees == Decimal("0.225")  # 0.09 + 0.135, accumulated per trade
    assert view.realized_pnl == Decimal("100")  # 2 x (150 - 100), gross of the fees
    assert view.entry_price == Decimal("0")  # closed flat: no fee loaded into a basis
    assert portfolio.account().cash == Decimal("100099.775")  # 100000 + 100 - 0.225


def test_funding_accrues_on_its_own_line_and_moves_cash_by_the_amount() -> None:
    """Funding's Tier-1 line beside the fee's, and the same three exclusions.

    ``cash += amount`` here where the fee's rule is ``cash -= fee``: the two
    mirror venue fields with opposite raw signs, so a payment (``< 0``) leaves
    less collateral and a credit (``> 0``) leaves more, with neither reaching
    ``entry_price`` nor ``realized_pnl`` (ADR-0037, ADR-0042 §4).

    The position is open and untouched by any second fill, so every field but
    ``funding`` and ``cash`` reads exactly what the opening fill left.
    """
    projection = _projection("100000")
    portfolio = projection.for_strategy("alpha")
    book_fill(projection, _fill(trade_id="f1", quantity="2", price="100"), side=Side.BUY)

    projection.apply_funding(_accrual(amount="-10", boundary=_HOUR))

    view = portfolio.position("BTC")
    assert view is not None
    assert view.funding == Decimal("-10")
    assert view.entry_price == Decimal("100")  # the traded price, carrying no funding
    assert view.realized_pnl == Decimal("0")  # gross, and nothing has closed anyway
    assert view.fees == Decimal("0")  # funding is not a fee and never lands on that line
    assert portfolio.account().cash == Decimal("99990")  # 100000 - 10, paid out


def test_an_accrual_at_or_below_the_stored_mark_is_dropped_whole() -> None:
    """The durable gate, and it drops the accrual *entirely* (ADR-0043 §5.2).

    The mark is what an in-memory key set cannot be — it survives the process —
    and it is read on both paths: it gates live's re-delivered history and a
    replay rerun, which re-derives every boundary in the file against a store
    that already holds the ledger. Ungated, each of those would apply a second
    time and the funding line and ``cash`` would double-count.

    "At or below", not "below": the mark records a boundary **already applied**,
    so re-offering it must change nothing. And the drop is whole — no line, no
    cash, no ``FundingChange`` to write — because a half-dropped accrual that
    still moved cash is the failure the symbol grain exists to prevent.
    """
    store = SQLiteStore(":memory:")
    projection = _projection("100000", store=store)
    portfolio = projection.for_strategy("alpha")
    book_fill(projection, _fill(trade_id="f1", quantity="2", price="100"), side=Side.BUY)
    # The ledger already carries the first two boundaries, as it would after the
    # crash that a re-delivery follows.
    store.checkpoint_ledger(
        account=Account.restore(
            account_id="paper-default",
            genesis_collateral=Decimal("100000"),
            genesis_ts_ns=7,
            cash=Decimal("100000"),
        ),
        funding_mark=("BTC", 2 * _HOUR),
        ts_ns=7,
    )

    at_the_mark = projection.apply_funding(_accrual(amount="-10", boundary=2 * _HOUR))
    below_the_mark = projection.apply_funding(_accrual(amount="-10", boundary=_HOUR))

    assert at_the_mark is None
    assert below_the_mark is None
    view = portfolio.position("BTC")
    assert view is not None
    assert view.funding == Decimal("0")
    assert portfolio.account().cash == Decimal("100000")


def test_the_boundary_above_the_mark_applies_and_carries_the_advance_with_it() -> None:
    """The mark travels *with* the funding line, never behind it (ADR-0043 §5.2).

    The advance rides the same ``FundingChange`` the line does, so the one
    ``checkpoint_ledger`` that makes the payment durable is the one that records
    it as applied. Handing them to the store separately is what would let a crash
    land between them, and the whole point of the mark is that it can never
    disagree with the sum it guards.

    So this asserts the pairing at the seam that produces it: the change carries
    the advance, at this accrual's own boundary and at symbol grain.
    """
    store = SQLiteStore(":memory:")
    projection = _projection("100000", store=store)
    book_fill(projection, _fill(trade_id="f1", quantity="2", price="100"), side=Side.BUY)

    change = projection.apply_funding(_accrual(amount="-10", boundary=3 * _HOUR))

    assert change is not None
    assert change.funding_mark == ("BTC", 3 * _HOUR)
    assert [position.symbol for position in change.positions] == ["BTC"]
    assert change.account.cash == Decimal("99990")


def test_one_accrual_splits_across_every_partition_holding_the_symbol() -> None:
    """Attribution is pro-rata by signed size, and sums to the amount exactly.

    Two strategies hold BTC — 3 long and 1 short, a net of 2 — and the account
    pays 10 against that net. Pro-rata reproduces each partition's own accrual
    from the net one, which is why the short *receives* 5 out of a payment the
    account made: at the same price and rate it was owed funding while the
    account on balance paid.

    The sum is asserted beside the shares because it is the property, not the
    arithmetic: ``Σ funding lines`` must equal the ``cash`` movement, or the
    ledger drifts a little further every boundary with nothing to correct it.
    """
    projection = _projection("100000")
    book_fill(projection, _fill(trade_id="f1", quantity="3", price="100"), side=Side.BUY)
    book_fill(
        projection,
        _fill(trade_id="f2", quantity="1", price="100", strategy_id="beta"),
        side=Side.SELL,
    )

    projection.apply_funding(_accrual(amount="-10", boundary=_HOUR))

    alpha = projection.for_strategy("alpha").position("BTC")
    beta = projection.for_strategy("beta").position("BTC")
    assert alpha is not None and beta is not None
    assert alpha.funding == Decimal("-15")  # -10 x (+3 / +2)
    assert beta.funding == Decimal("5")  # -10 x (-1 / +2)
    assert alpha.funding + beta.funding == Decimal("-10")
    assert projection.account().cash == Decimal("99990")


def test_an_accrual_with_no_partition_to_attribute_to_still_moves_cash() -> None:
    """The payment is a fact about the **account**; the attribution is separate.

    Cash moves by the full amount even where there is no open partition to file
    it against, because those are two different questions and only the first one
    is about whether the payment happened. The consequence is deliberate and
    worth pinning: ``Σ funding lines`` is *short* of the ``cash`` movement in
    exactly this case, which is the one place the two are allowed to disagree.

    Unreachable on the paper path as wired today — paper generates an accrual
    only for a symbol its own ledger holds, and holding it means a partition — so
    the case that reaches here is foreign flow into the account, which is
    [#189](https://github.com/MarcosACH/tickwright/issues/189)'s reserved
    unattributed partition. Asserted now because the behaviour ships now: the
    branch is live the moment a live venue reports funding on a symbol this
    engine never traded.
    """
    projection = _projection("100000")

    change = projection.apply_funding(_accrual(amount="-10", boundary=_HOUR))

    assert change is not None
    assert change.positions == ()  # nothing to attribute to, and nothing invented
    assert change.funding_mark == ("BTC", _HOUR)  # still applied, so still marked
    assert projection.account().cash == Decimal("99990")
    assert projection.for_strategy("alpha").position("BTC") is None


def test_a_maker_rebate_credits_the_cash_line_rather_than_debiting_it() -> None:
    # The sign convention is the whole of what makes a fee a credit (ADR-0036):
    # a negative fee is a rebate, and cash moving by its negative therefore
    # *adds*. Reached by a rebate volume tier, not by making liquidity — which is
    # why nothing here says "maker" and the projection never asks.
    projection = _projection("100000")
    portfolio = projection.for_strategy("alpha")

    book_fill(
        projection, _fill(trade_id="f1", quantity="2", price="100", fee="-0.03"), side=Side.BUY
    )

    view = portfolio.position("BTC")
    assert view is not None
    assert view.fees == Decimal("-0.03")
    assert portfolio.account().cash == Decimal("100000.03")


def test_a_redelivered_fill_charges_its_fee_once() -> None:
    # The fee rides the same gatekeeper the position does — ``Position.apply``
    # dedups on ``event_id`` — so at-least-once delivery cannot charge it twice.
    # Asserted on an *adding* fill, where a second application would otherwise
    # leave both the size and the fee line silently doubled.
    projection = _projection("100000")
    portfolio = projection.for_strategy("alpha")
    fill = _fill(trade_id="f1", quantity="2", price="100", fee="0.09")

    book_fill(projection, fill, side=Side.BUY)
    book_fill(projection, fill, side=Side.BUY)

    view = portfolio.position("BTC")
    assert view is not None
    assert view.size == Decimal("2")
    assert view.fees == Decimal("0.09")
    assert portfolio.account().cash == Decimal("99999.91")


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


def test_a_live_ledger_materialises_at_the_figures_the_venue_reported() -> None:
    """The other half of the seed's absence: the live row is *materialised* from
    the venue's own account read at the startup barrier (ADR-0043 §6).

    The write is the same ``checkpoint_ledger(account=…)`` paper's genesis takes,
    and it lands for the same reason — so that ``account()`` has a cash line
    before a strategy is ever let near one — but the number comes from the venue
    rather than from config, which is why it cannot be written three steps
    earlier where paper's is.
    """
    store = SQLiteStore(":memory:")
    projection = _projection(None, store=store)
    projection.recover()
    assert projection.is_opened() is False  # nothing restored, and live seeds nothing

    projection.materialise(account_state("25.9264", "-0.034"))

    assert projection.is_opened() is True
    assert projection.account().cash == Decimal("25.9604")
    row = store.load_account()
    assert row is not None
    assert row.account_id == "hyperliquid-testnet-0xabc"
    assert row.genesis_collateral == Decimal("25.9604")
    assert row.cash == Decimal("25.9604")
    assert row.genesis_ts_ns == 7  # the run's clock, not the venue's own stamp


def test_a_second_materialisation_is_refused_rather_than_moving_the_genesis() -> None:
    """Genesis is written once (ADR-0042 §3), and the verb that would corrupt it
    is where that rule is stated (ADR-0047 §1).

    On live nothing could ever catch a second write after the fact: the value is
    *provenance only* — there is no configured counterpart to compare it against,
    so #188's genesis refusal is paper-only by ADR-0043 §10's predicate. The
    barrier's caller skips this verb on a ledger that is already open, but that
    check answers a different question — "do I owe the venue a read?" — and a
    rule enforced only by the one caller that exists today is a rule the next
    caller inherits nothing from. The equity handed in here has moved, which is
    exactly the legitimate change (a deposit, or the same position marked
    differently) that must not rewrite an opening declaration.
    """
    store = SQLiteStore(":memory:")
    projection = _projection(None, store=store)
    projection.recover()
    projection.materialise(account_state("25.9264", "-0.034"))

    with pytest.raises(InvariantViolation):
        projection.materialise(account_state("99999", "-0.034"))

    # Neither half moved: the refusal lands ahead of the in-memory assignment, so
    # a caught violation cannot leave the ledger reporting a cash line the store
    # never recorded. ``cash`` is what a reader would see move — ``Account.ingest``
    # opens the line *at* genesis, so a second one resets it too.
    assert projection.account().cash == Decimal("25.9604")
    row = store.load_account()
    assert row is not None
    assert row.genesis_collateral == Decimal("25.9604")


def test_a_ledger_recorded_against_another_account_is_refused() -> None:
    """The first of the surface's three refusals (ADR-0043 §10): the stored row
    and ``account_spec()`` are two declarations about one account, and a restart
    that finds them disagreeing has been pointed at somebody else's history.

    ``account_id`` compares on **both** paths — it is the venue-qualified identity
    ADR-0038 defines, declared rather than ingested on either — so this is the one
    condition that is not gated on the genesis predicate.
    """
    store = SQLiteStore(":memory:")
    store.checkpoint_ledger(
        account=Account.restore(
            account_id="paper-other",
            genesis_collateral=Decimal("50000"),
            genesis_ts_ns=7,
            cash=Decimal("50000"),
        ),
        ts_ns=2_000,
    )

    with pytest.raises(StoreAccountMismatch) as refusal:
        _projection("50000", store=store).recover()

    # Named, not merely counted: the operator's remedy is to point the run at a
    # fresh store or restore the declared value, and neither is choosable without
    # knowing which field disagreed and what the two sides said.
    assert "account_id" in str(refusal.value)
    assert "paper-other" in str(refusal.value)
    assert "paper-default" in str(refusal.value)


def test_a_changed_paper_genesis_against_a_recorded_ledger_is_refused() -> None:
    """A different opening balance is a different account history (ADR-0042 §3).

    Genesis is written once and never re-derived, so an operator who edits
    ``TICKWRIGHT_PAPER__GENESIS_COLLATERAL`` against a store that already holds a
    ledger is asking for two irreconcilable things: the equity and effective
    leverage that ledger's cash line was accrued against, and a different capital
    base to measure them from. Silently keeping the stored number would report
    against capital nobody chose, which is the very failure ADR-0042 §1 refuses a
    *default* collateral to prevent.
    """
    store = SQLiteStore(":memory:")
    store.checkpoint_ledger(
        account=Account.restore(
            account_id="paper-default",
            genesis_collateral=Decimal("50000"),
            genesis_ts_ns=7,
            cash=Decimal("49000"),
        ),
        ts_ns=2_000,
    )

    with pytest.raises(StoreAccountMismatch) as refusal:
        _projection("75000", store=store).recover()

    assert "genesis_collateral" in str(refusal.value)
    assert "50000" in str(refusal.value)
    assert "75000" in str(refusal.value)


def test_two_disagreeing_fields_are_reported_in_one_refusal() -> None:
    """Every disagreeing field at once, not one per restart (ADR-0042 §3).

    An operator who edited both the label and the collateral fixes what the first
    restart told them about, restarts, and is refused again for the other — twice
    the downtime for one mistake, and each cycle looks like a *new* fault rather
    than the remainder of the known one.
    """
    store = SQLiteStore(":memory:")
    store.checkpoint_ledger(
        account=Account.restore(
            account_id="paper-other",
            genesis_collateral=Decimal("50000"),
            genesis_ts_ns=7,
            cash=Decimal("49000"),
        ),
        ts_ns=2_000,
    )

    with pytest.raises(StoreAccountMismatch) as refusal:
        _projection("75000", store=store).recover()

    # One field per line, both sides on it, and the remedy last — ADR-0042 §3's
    # own layout. Read as a single blob, two mismatches are a sentence an operator
    # has to parse; laid out, the report is a diff they can act on.
    lines = str(refusal.value).splitlines()
    assert [line.split(":")[0].strip() for line in lines[1:3]] == [
        "account_id",
        "genesis_collateral",
    ]
    assert "paper-other" in lines[1] and "paper-default" in lines[1]
    assert "50000" in lines[2] and "75000" in lines[2]
    assert "fresh" in lines[-1] or "fresh" in lines[-2]


def test_a_genesis_that_round_tripped_to_another_representation_still_agrees() -> None:
    """The comparison is on value, not on the string the store handed back.

    Money is persisted as ``TEXT`` and is exact in *representation* (ADR-0043 §7),
    so trailing zeros and exponent forms survive the round trip — and an operator
    who wrote ``100000`` against a row recorded as ``1.00000E+5`` has changed
    nothing about the account. Refusing that would make the check fire on the
    store's own faithfulness.
    """
    store = SQLiteStore(":memory:")
    store.checkpoint_ledger(
        account=Account.restore(
            account_id="paper-default",
            genesis_collateral=Decimal("1.00000E+5"),
            genesis_ts_ns=7,
            cash=Decimal("100000"),
        ),
        ts_ns=2_000,
    )

    projection = _projection("100000", store=store)
    projection.recover()

    assert projection.account().cash == Decimal("100000")


def test_a_paper_store_with_order_history_and_no_ledger_is_refused() -> None:
    """The upgrade hazard, refused rather than backfilled (ADR-0043 §8).

    A paper store predating the ledger carries an ``orders`` table full of filled
    sagas. Left to seed, it would open a fresh ledger and report a flat account at
    full cash — silently ignoring every fill it ever recorded. And it cannot be
    reconstructed from those orders: that is event-sourced recovery through the
    back door (ADR-0009), and it would not even work, because the per-fill fee
    only exists on the event as of ADR-0036 and funding did not exist at all. The
    fee and funding lines would be permanently zero and wrong — money invented as
    never-charged. A loud failure is the only honest outcome.
    """
    store = SQLiteStore(":memory:")
    _record_order_history(store)

    with pytest.raises(StoreAccountMismatch):
        _projection("100000", store=store).recover()

    # No backfill and no seed: the refusal lands ahead of the genesis write, so a
    # caught violation cannot leave behind the very fabricated ledger it refused.
    assert store.load_account() is None


def test_a_live_store_predating_the_ledger_starts_rather_than_being_refused() -> None:
    """The ledger-less refusal is **paper-only**, and the gate is not optional
    (ADR-0043 §8).

    On live the same store — orders, no account row — is legitimate and
    self-correcting: the barrier materialises the row from the venue's own account
    read and positions heal from venue truth. Applied unconditionally, the
    refusal would brick every live upgrade from a pre-ledger store, which is a
    state the ADR calls legitimate one sentence above the refusal.
    """
    store = SQLiteStore(":memory:")
    _record_order_history(store)

    _projection(None, store=store).recover()

    # Nothing seeded either: live's row is the barrier's to materialise.
    assert store.load_account() is None


def test_a_live_restart_is_not_refused_for_the_genesis_it_never_declared() -> None:
    """The genesis comparison is gated on the same predicate, and this is the
    direction that would hurt most (ADR-0043 §10).

    Live's stored genesis is ``equity − Σ unrealized_pnl``, ingested at the
    barrier; its declared genesis is ``None``, because there is no configured
    counterpart to check against. Ungated, the two differ on every start after the
    first — a check whose whole purpose is catching a swapped account would instead
    fail-fast every live restart. ``account_id`` still compares here, and agrees.
    """
    store = SQLiteStore(":memory:")
    store.checkpoint_ledger(
        account=Account.restore(
            account_id="hyperliquid-testnet-0xabc",
            genesis_collateral=Decimal("25.9604"),
            genesis_ts_ns=7,
            cash=Decimal("31.5"),
        ),
        ts_ns=2_000,
    )
    projection = _projection(None, store=store)

    projection.recover()

    assert projection.account().cash == Decimal("31.5")


def test_a_live_ledger_recorded_against_another_address_is_still_refused() -> None:
    """``account_id`` compares on **both** paths (ADR-0042 §6, ADR-0038).

    It is the venue-qualified identity, declared rather than ingested on either
    venue, so it is the one field live has a counterpart for — and a live engine
    pointed at another address's ledger would otherwise restore that account's
    cash line and heal *this* account's positions into it.
    """
    store = SQLiteStore(":memory:")
    store.checkpoint_ledger(
        account=Account.restore(
            account_id="hyperliquid-testnet-0xdef",
            genesis_collateral=Decimal("25.9604"),
            genesis_ts_ns=7,
            cash=Decimal("31.5"),
        ),
        ts_ns=2_000,
    )

    with pytest.raises(StoreAccountMismatch) as refusal:
        _projection(None, store=store).recover()

    message = str(refusal.value)
    assert "account_id" in message
    assert "genesis_collateral" not in message  # nothing declared to disagree with
