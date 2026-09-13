"""What crosses the ``Exchange`` seam that is not an event.

The venue-neutral order request the engine hands to ``Exchange.place``, and the
answers the reconciler's direct reads come back with (ADR-0004): one order's
record and fill history, the account snapshot, the mode verdict, and how a read
failed. None of these is ever published on the bus (ADR-0045 §1 closes that
catalog). They are value types, frozen like the events but keyed by nothing,
and they live together so that "what a venue is asked and what it answers" is
one module rather than a section of ``events.py``.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from .enums import OrderType, Side, TimeInForce
from .events import FillReport, OrderStatusReport
from .leverage import LeverageSpec
from .order import Order

# --- Venue truth for one cloid (a query result, not an event) ---------------


class VenueReadFailure(Enum):
    """How an in-flight venue read failed — the two transient outcomes of
    ADR-0048's taxonomy, told apart (ADR-0049).

    Both are still "not venue truth", which is the whole of ADR-0011 inv 1: a
    failed read is never a view and never an empty book. What they differ on is
    **whether the venue answered at all**, and that is the only thing a caller
    needs to know to decide how much of its worklist a single failure should
    cost:

    - ``SEND_FAILED`` — no body arrived. The venue may be unreachable, so every
      other order in the pass would pay a full request timeout to learn the
      same thing. The pass stops here.
    - ``UNREADABLE_BODY`` — a body arrived and could not be read. The venue is
      up and answering at full speed; the next order's read is one ordinary
      round-trip away, and the unreadable one says nothing about it. Only that
      order is skipped.

    Deliberately not a third state on ``VenueOrderView``: a view is a
    *successful* read, and a failure that could be carried inside one would be
    one `if` away from being read as an empty book — which is precisely what
    inv 1 forbids and what the ``None`` this replaces guaranteed by not being a
    view at all.
    """

    SEND_FAILED = "send_failed"
    UNREADABLE_BODY = "unreadable_body"


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderRef:
    """What a venue read needs to find one order (``Exchange.fetch_order``).

    The cloid is the key the venue's order record answers to. The oid and the
    ack time are for after the venue has dropped that record: it drops order
    records by count but keeps fills for years, so the fill history is read by
    the ack's oid from the ack time (ADR-0011 inv 2). Both are ``None`` for a
    saga whose ack never arrived. That saga has no fill history to consult.
    """

    cloid: str
    symbol: str
    venue_oid: str | None = None
    acked_ts_ns: int | None = None

    @classmethod
    def of(cls, order: Order) -> "OrderRef":
        """Everything the venue may need to find ``order``, taken from the saga."""
        return cls(
            cloid=order.cloid,
            symbol=order.symbol,
            venue_oid=order.venue_oid,
            acked_ts_ns=order.acked_ts_ns,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class VenueOrderView:
    """One *successful* venue read for a cloid (``Exchange.fetch_order``).

    Bundles the venue's order record (``status``; ``None`` when the venue
    positively has no record) with that cloid's fill history, so the ADR-0011
    open-orders-plus-fill-history cross-check is one read: a view with no
    status and no fills is proof the order never landed. A read that *failed*
    is never a view — ``fetch_order`` returns a ``VenueReadFailure`` for that
    (inv 1: an outage must never read as "no record").
    """

    status: OrderStatusReport | None
    fills: tuple[FillReport, ...] = ()

    @property
    def has_record(self) -> bool:
        """Whether the venue knows this cloid at all — the resend gate (ADR-0008)."""
        return self.status is not None or bool(self.fills)


# --- Venue truth for the account (a query result, not an event) --------------


@dataclass(frozen=True, slots=True, kw_only=True)
class VenuePositionState:
    """One position inside a successful venue account read, normalized.

    ``signed_size`` carries the direction the venue reports it with — positive
    long, negative short — where our own ledger keeps a magnitude and rides the
    side on the saga.

    ``isolated_collateral`` is the position's own locked bucket, and its
    ``None`` is what says the position is **cross**: a cross position is backed
    by the account pool and has no bucket of its own, while an isolated one
    always has a positive number here. Its counterpart ``margin_used`` moves
    with the mark on both modes, which is why it sits inside the divergence
    band rather than being the same constant on both sides (ADR-0040 §3, as
    corrected).

    ``liquidation_price`` is the venue's own number read through rather than
    recomputed, and its ``None`` is the **majority** case for a long, not a
    corner: the venue omits the field whenever the price would be non-positive,
    which happens once collateral is large relative to notional and is
    structurally impossible for a short (ADR-0046 §6). Nothing may substitute a
    value for it — a frozen absence beats a fabricated price (ADR-0034).

    ``leverage`` is the venue's **stored setting** for the symbol, not a figure
    derived from the position: the two travel together on the snapshot but only
    one of them moves with the mark. It is what the post-boot drift check
    compares against config (ADR-0044 §10), and it is carried here rather than
    recovered downstream because nothing else on the row implies it — a leverage
    change never re-margins an open position, so ``margin_used`` keeps whatever
    leverage the position opened at. It has **no default**, on this class's own
    terms: a defaulted pair would let a snapshot claim a setting no venue was
    read for, and against the commonest config that fabrication reads as
    agreement.
    """

    symbol: str
    signed_size: Decimal
    entry_price: Decimal | None
    notional: Decimal
    unrealized_pnl: Decimal
    margin_used: Decimal
    isolated_collateral: Decimal | None
    liquidation_price: Decimal | None
    leverage: LeverageSpec


@dataclass(frozen=True, slots=True, kw_only=True)
class VenueAccountState:
    """One *successful* venue account read (``Exchange.fetch_account_state``).

    The account-grain half of the reconcile's cross-check, already normalized:
    every field is a ``domain`` quantity, and which venue field each came from
    is the adapter's knowledge alone (ADR-0045 §3). A read that *failed* is
    never a state — ``fetch_account_state`` returns ``None`` for that, the same
    inv-1 guard ``VenueOrderView`` carries: an outage must never read as a flat
    book.

    ``cross_maintenance_margin`` is named for the **subset** it covers, not for
    the quantity: the venue publishes maintenance margin over cross positions
    only, so it cross-checks the cross subset while our own reported figure is a
    Σ over every position (ADR-0046 §2.1). Isolated maintenance has no venue
    counterpart at all. Free margin is deliberately *not* the venue's
    withdrawable figure, which additionally deducts margin reserved by resting
    orders — the normal state of a running engine, and a gap no tolerance
    absorbs (ADR-0046 §2).
    """

    equity: Decimal
    free_margin: Decimal
    cross_maintenance_margin: Decimal
    positions: tuple[VenuePositionState, ...] = ()


class AccountModeVerdict(Enum):
    """Whether the venue still reports the account in a mode whose account-grain
    numbers this engine may heal toward (``Exchange.verify_account_mode``).

    The verdict and not the mode: which literals a venue accepts is venue
    knowledge and stays in the adapter (ADR-0031), while what the caller has to
    decide is whether the snapshot it just read still means what it meant at
    boot (ADR-0046 §4).

    Three values rather than a ``bool``, because the alert has to say **why** it
    stopped: an operator told only that the mode is unverified cannot tell an
    account somebody switched from one the engine could not reach.

    - ``VERIFIED`` — the venue answered with a mode the adapter accepts.
    - ``CHANGED`` — the venue answered with one it does not. In flight this is
      always a change, since boot refused to start on anything else.
    - ``UNREADABLE`` — the read failed, timed out, or came back a shape that is
      not a mode at all.

    The last two are one branch at every caller and stay two values here for the
    record alone: an unverified mode is not evidence that it is unchanged, so
    the guard fails closed on both (ADR-0046 §4's in-flight twin of §3's "never
    assume standard on error"). Collapsing them into a single ``UNVERIFIED``
    would cost nothing in control flow and lose the one thing an operator reads.
    """

    VERIFIED = "verified"
    CHANGED = "changed"
    UNREADABLE = "unreadable"


# --- Venue-neutral order request (not an event) -----------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class PlaceOrder:
    """The venue-neutral order the ``ExecutionManager`` hands to ``Exchange.place``.

    Carries the engine-assigned ``cloid`` (the venue-facing identity) plus the
    order parameters; unlike a ``Signal`` it carries no strategy sequence, and
    unlike an ``Event`` it is never published on the bus.
    """

    cloid: str
    symbol: str
    side: Side
    quantity: Decimal
    order_type: OrderType
    time_in_force: TimeInForce
    price: Decimal | None = None
    post_only: bool = False
