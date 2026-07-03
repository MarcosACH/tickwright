"""Domain event envelope + ADR-0025 idempotency-key derivations.

These tests pin the load-bearing contract of the event schema: `partition_key`
(the bus's ordering key) and `event_id` (the dedup key) are deterministic, pure
functions of an event's own domain fields — provenance-free (ADR-0025), so a
reconciler-synthesized event and a venue-pushed duplicate collapse to one id.
"""

from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from tickwright.domain import (
    AggressorSide,
    FillReport,
    MarketTick,
    OrderFilled,
    OrderPlaced,
    OrderState,
    OrderStatusReport,
    OrderSubmitted,
    OrderType,
    PlaceSignal,
    Side,
    TimeInForce,
)


def test_market_tick_event_id_and_partition_key() -> None:
    tick = MarketTick(
        ts_event=1_000,
        ts_init=1_000,
        symbol="BTC",
        price=Decimal("42000.5"),
        size=Decimal("0.1"),
        aggressor_side=AggressorSide.BUY,
        trade_id="t1",
        seq=7,
    )
    # Replay-form weak key (ADR-0027): {symbol}:{ts_event}:{seq}.
    assert tick.event_id == "BTC:1000:7"
    assert tick.partition_key == "BTC"


def test_place_signal_event_id_is_the_signal_id() -> None:
    signal = PlaceSignal(
        ts_event=2_000,
        ts_init=2_000,
        strategy_id="trivial",
        symbol="ETH",
        seq=3,
        side=Side.BUY,
        quantity=Decimal("1"),
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.IOC,
    )
    # ADR-0006 / ADR-0025: event_id == signal_id == {strategy_id}:{symbol}:{seq}.
    assert signal.signal_id == "trivial:ETH:3"
    assert signal.event_id == "trivial:ETH:3"
    assert signal.partition_key == "ETH"


def test_order_lifecycle_event_ids_are_single_entry_per_state() -> None:
    placed = OrderPlaced(
        ts_event=3_000,
        ts_init=3_000,
        cloid="0xabc",
        strategy_id="trivial",
        signal_id="trivial:BTC:1",
        symbol="BTC",
    )
    submitted = OrderSubmitted(
        ts_event=3_000,
        ts_init=3_000,
        cloid="0xabc",
        strategy_id="trivial",
        signal_id="trivial:BTC:1",
        symbol="BTC",
    )
    assert placed.event_id == "0xabc:pending"
    assert submitted.event_id == "0xabc:submitted"
    assert placed.partition_key == "BTC"
    assert submitted.partition_key == "BTC"


def test_fill_family_event_ids_are_provenance_free() -> None:
    fill_report = FillReport(
        ts_event=4_000,
        ts_init=4_000,
        cloid="0xabc",
        symbol="BTC",
        trade_id="v42",
        quantity=Decimal("0.1"),
        price=Decimal("42000"),
    )
    order_filled = OrderFilled(
        ts_event=4_000,
        ts_init=4_000,
        cloid="0xabc",
        strategy_id="trivial",
        signal_id="trivial:BTC:1",
        symbol="BTC",
        trade_id="v42",
        quantity=Decimal("0.1"),
        price=Decimal("42000"),
        cum_qty=Decimal("0.1"),
    )
    # Both derive {cloid}:fill:{trade_id} so a healed fill and a venue duplicate
    # of the same trade collapse to one id (ADR-0025 rule 1).
    assert fill_report.event_id == "0xabc:fill:v42"
    assert order_filled.event_id == "0xabc:fill:v42"


def test_order_status_report_event_id_is_single_entry_per_state() -> None:
    report = OrderStatusReport(
        ts_event=4_000,
        ts_init=4_000,
        cloid="0xabc",
        symbol="BTC",
        status=OrderState.LIVE,
        venue_oid="oid-9",
    )
    # ADR-0025: the status half of the report split keys {cloid}:{state}, the
    # same single-entry-per-state key as the OrderEvent it will be turned into.
    assert report.event_id == "0xabc:live"
    assert report.partition_key == "BTC"


def test_reconciliation_flag_is_excluded_from_the_event_id() -> None:
    def _filled(*, reconciliation: bool) -> OrderFilled:
        return OrderFilled(
            ts_event=5_000,
            ts_init=5_000,
            cloid="0xabc",
            strategy_id="trivial",
            signal_id="trivial:BTC:1",
            symbol="BTC",
            trade_id="v42",
            quantity=Decimal("0.1"),
            price=Decimal("42000"),
            cum_qty=Decimal("0.1"),
            reconciliation=reconciliation,
        )

    # A venue-pushed fill and a reconciler-healed copy collapse to one id.
    assert _filled(reconciliation=False).event_id == _filled(reconciliation=True).event_id


# ---- Property tests: the derivations hold for arbitrary field values --------

_ids = st.text(
    # Visible ASCII minus the ':' key separator — the id components in practice.
    alphabet=st.characters(min_codepoint=33, max_codepoint=126, exclude_characters=":"),
    min_size=1,
    max_size=12,
)
_ts = st.integers(min_value=0, max_value=2**63 - 1)
_seq = st.integers(min_value=0, max_value=2**31)


@given(symbol=_ids, ts_event=_ts, seq=_seq, trade_id=_ids)
def test_market_tick_key_derivation(symbol: str, ts_event: int, seq: int, trade_id: str) -> None:
    tick = MarketTick(
        ts_event=ts_event,
        ts_init=ts_event,
        symbol=symbol,
        price=Decimal("1"),
        size=Decimal("1"),
        aggressor_side=AggressorSide.BUY,
        trade_id=trade_id,
        seq=seq,
    )
    assert tick.event_id == f"{symbol}:{ts_event}:{seq}"
    assert tick.partition_key == symbol


@given(strategy_id=_ids, symbol=_ids, seq=_seq)
def test_signal_key_derivation(strategy_id: str, symbol: str, seq: int) -> None:
    signal = PlaceSignal(
        ts_event=1,
        ts_init=1,
        strategy_id=strategy_id,
        symbol=symbol,
        seq=seq,
        side=Side.SELL,
        quantity=Decimal("1"),
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.IOC,
    )
    assert signal.event_id == f"{strategy_id}:{symbol}:{seq}"
    assert signal.partition_key == symbol


@given(cloid=_ids, symbol=_ids, trade_id=_ids)
def test_fill_key_derivation(cloid: str, symbol: str, trade_id: str) -> None:
    fill = FillReport(
        ts_event=1,
        ts_init=1,
        cloid=cloid,
        symbol=symbol,
        trade_id=trade_id,
        quantity=Decimal("1"),
        price=Decimal("1"),
    )
    assert fill.event_id == f"{cloid}:fill:{trade_id}"
    assert fill.partition_key == symbol
