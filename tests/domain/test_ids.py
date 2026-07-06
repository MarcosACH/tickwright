"""``SignalId`` — the one owner of the ``signal_id`` wire format (ADR-0006).

The deterministic identity ``{strategy_id}:{symbol}:{seq}`` is authored in exactly
one place and parsed back in exactly one place, so recovery and the wire form can
never drift. These tests pin both directions and the round-trip that ties them.
"""

import pytest

from tickwright.domain import PlaceSignal, SignalId
from tickwright.domain.enums import OrderType, Side, TimeInForce


def _place(strategy_id: str, symbol: str, seq: int) -> PlaceSignal:
    return PlaceSignal(
        ts_event=1,
        ts_init=1,
        strategy_id=strategy_id,
        symbol=symbol,
        seq=seq,
        side=Side.BUY,
        quantity=1,  # type: ignore[arg-type]
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.IOC,
    )


def test_render_is_the_documented_format() -> None:
    assert SignalId("alpha", "BTC", 7).render() == "alpha:BTC:7"


def test_parse_recovers_the_three_fields() -> None:
    parsed = SignalId.parse("alpha:BTC:7")
    assert parsed == SignalId("alpha", "BTC", 7)
    assert parsed.strategy_id == "alpha"
    assert parsed.symbol == "BTC"
    assert parsed.seq == 7


def test_render_parse_round_trips() -> None:
    original = SignalId("alpha", "ETH", 42)
    assert SignalId.parse(original.render()) == original


def test_signal_id_property_and_value_object_agree() -> None:
    # Signal.signal_id must delegate to SignalId — a single owner of the format.
    signal = _place("alpha", "BTC", 3)
    assert signal.signal_id == SignalId("alpha", "BTC", 3).render()
    assert SignalId.parse(signal.signal_id).seq == 3


def test_parse_tolerates_a_colon_in_the_strategy_id() -> None:
    # seq is always the final field and symbol the one before it; only those two
    # are colon-free. A strategy_id that itself contains a colon still parses —
    # the naive left-to-right ``split(":")`` this replaces would have corrupted it.
    parsed = SignalId.parse("team:alpha:BTC:9")
    assert parsed == SignalId("team:alpha", "BTC", 9)
    assert parsed.seq == 9


def test_parse_rejects_a_malformed_id() -> None:
    with pytest.raises(ValueError):
        SignalId.parse("no-separators")
