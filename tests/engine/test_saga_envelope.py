"""The saga envelope — every field an ``OrderEvent`` carries but its family's own.

``tests/engine/test_execution_manager.py`` keeps the manager, and reaches these
fields only as they come out the far end of a published event. That is the right
place for what a strategy sees, and the wrong place to state a *rule*: the
envelope's one non-obvious clause — a fresher ``venue_oid`` wins, otherwise the
saga's own is kept — is consulted by three builders on three different paths, so
asserted there it is asserted three times and defined nowhere.

Here it is one function, driven directly. The module-private import is the same
licence ``test_ws_shim.py`` takes: the subject *is* the private, and routing the
case through a public caller would test the caller.
"""

from dataclasses import fields
from decimal import Decimal

from tickwright.domain import Order, OrderEvent, OrderState, Side
from tickwright.domain.enums import OrderType
from tickwright.engine.execution import _saga_envelope

_NOW_NS = 1_700_000_000_000_000_000


def _order(*, venue_oid: str | None) -> Order:
    return Order.restore(
        cloid="0xcafe",
        strategy_id="trivial",
        signal_id="trivial:BTC:1",
        symbol="BTC",
        side=Side.BUY,
        quantity=Decimal("0.5"),
        order_type=OrderType.LIMIT,
        state=OrderState.LIVE,
        cum_qty=Decimal("0"),
        venue_oid=venue_oid,
        reason=None,
        cancel_requested=False,
        cancel_requested_ts=None,
        cancel_signal_id=None,
        applied_event_ids=(),
    )


def test_a_fresher_venue_oid_wins_and_an_absent_one_keeps_the_sagas_own() -> None:
    """The envelope's one rule, and the reason it is one function.

    Both arms off **one** order, because the assertion is about which side was
    preferred and a single call cannot show that: the saga carries ``0xack``
    throughout, so an envelope reading ``0xfresh`` can only have taken the
    argument and one reading ``0xack`` can only have fallen back.

    Verified discriminating rather than assumed: against an envelope that takes
    the argument unconditionally the second arm fails on ``None``, and against
    one that always reads the order the first fails on ``0xack``.
    """
    acked = _order(venue_oid="0xack")

    fresher = _saga_envelope(acked, now_ns=_NOW_NS, venue_oid="0xfresh")
    absent = _saga_envelope(acked, now_ns=_NOW_NS, venue_oid=None)

    assert fresher["venue_oid"] == "0xfresh"
    assert absent["venue_oid"] == "0xack"


def test_an_order_the_venue_never_acked_has_no_oid_to_fall_back_on() -> None:
    """The rule's floor: falling back is not the same as inventing one.

    The ``DENIED`` shape — refused by the guard, never sent — so neither side has
    an oid and the envelope must say so rather than reach for a placeholder.
    """
    envelope = _saga_envelope(_order(venue_oid=None), now_ns=_NOW_NS, venue_oid=None)

    assert envelope["venue_oid"] is None


def test_the_envelope_carries_the_orders_identity_and_one_instant() -> None:
    """``ts_event`` and ``ts_init`` are the same instant, deliberately.

    A saga transition is a fact that occurs *here* — the engine is where it
    happens — so both stamps are the construction time (ADR-0005). The fill
    family is the exception and is built by ``Order.record_fill`` instead, which
    is why no fill event is assembled from this envelope.
    """
    envelope = _saga_envelope(_order(venue_oid="0xack"), now_ns=_NOW_NS)

    assert envelope["ts_event"] == _NOW_NS
    assert envelope["ts_init"] == _NOW_NS
    assert envelope["cloid"] == "0xcafe"
    assert envelope["strategy_id"] == "trivial"
    assert envelope["signal_id"] == "trivial:BTC:1"
    assert envelope["symbol"] == "BTC"


def test_reconciliation_provenance_is_off_unless_the_caller_says_otherwise() -> None:
    """ADR-0011 inv 6 is provenance, so the default has to be the honest one: a
    transition nobody flagged is a venue push, not a synthetic replica. It is
    defaulted rather than demanded because the guard's ``DENIED`` never has one
    to offer — it is minted here and never reconciled."""
    assert _saga_envelope(_order(venue_oid=None), now_ns=_NOW_NS)["reconciliation"] is False
    assert (
        _saga_envelope(_order(venue_oid=None), now_ns=_NOW_NS, reconciliation=True)[
            "reconciliation"
        ]
        is True
    )


def test_the_envelope_is_exactly_what_an_order_event_carries_beyond_its_family() -> None:
    """The membership rule, so the two cannot drift silently apart.

    ``OrderEvent``'s own declared fields plus ``Event``'s two stamps — no more,
    because a field only some families carry (``reason``) belongs to the builder
    that requires it, and no less, because a member left out is a member three
    builders go back to spelling for themselves. mypy already refuses a
    *missing* one at every construction site; what it cannot say is that the
    envelope has no business carrying anything else.
    """
    assert set(_saga_envelope(_order(venue_oid=None), now_ns=_NOW_NS)) == {
        field.name for field in fields(OrderEvent)
    }
