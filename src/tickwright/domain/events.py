"""The event schema: frozen dataclasses with provenance-free idempotency keys.

Every event is ``@dataclass(frozen=True, slots=True, kw_only=True)`` — immutable
(never mutated after dispatch), cheap, stdlib-only, and keyword-constructed so
the many-field lifecycle events stay readable and free of positional-argument
soup (ADR-0025).

Two envelope accessors are load-bearing and implemented as pure properties over
each event's own domain fields, so they are deterministic and provenance-free by
construction (ADR-0025):

* ``partition_key`` — the bus's ordering key. Every v1 event is symbol-scoped,
  so it returns ``symbol``; a future account-scoped event overrides the property
  without touching the bus (ADR-0003).
* ``event_id`` — the dedup key, derived per event family (see the table in
  ADR-0025). Because it reads only domain identity — never the ``reconciliation``
  provenance flag — a reconciler-synthesized fill and a venue-pushed duplicate of
  the same trade collapse to one id.
"""

from dataclasses import dataclass
from decimal import Decimal

from .enums import AggressorSide, OrderState, OrderType, Side, TimeInForce
from .ids import SignalId


@dataclass(frozen=True, slots=True, kw_only=True)
class Event:
    """The base envelope: when the fact occurred and when the object was built.

    Timestamps are UTC epoch nanoseconds (ADR-0005): ``ts_event`` is when the
    fact occurred, ``ts_init`` is ``Clock.timestamp_ns()`` at construction.
    """

    ts_event: int
    ts_init: int

    @property
    def event_id(self) -> str:
        """The dedup key. Implemented per family (ADR-0025)."""
        raise NotImplementedError

    @property
    def partition_key(self) -> str:
        """The bus's per-symbol ordering key. Implemented per family."""
        raise NotImplementedError


# --- Market data ------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class MarketTick(Event):
    """A last-trade tick from the ``trades`` channel (ADR-0027).

    Single-price (no book side): the paper exchange fills MARKET at ``price`` and
    a LIMIT when a later tick crosses it. ``seq`` is the feed's per-symbol source
    sequence; on the replay path it disambiguates the weak dedup key.
    """

    symbol: str
    price: Decimal
    size: Decimal
    aggressor_side: AggressorSide
    trade_id: str
    seq: int
    venue_trade_id: bool = False
    """Whether ``trade_id`` is a venue-assigned id (the live path, ADR-0027).
    Replay never sets it: a file-sourced trade id is not assumed unique."""

    @property
    def event_id(self) -> str:
        # Weak key (audit/log only, not a correctness key), ADR-0027: the live
        # form leans on the venue's own trade id; the replay form cannot.
        if self.venue_trade_id:
            return f"{self.symbol}:{self.trade_id}"
        return f"{self.symbol}:{self.ts_event}:{self.seq}"

    @property
    def partition_key(self) -> str:
        return self.symbol


# --- Signals (strategy intent) ----------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class Signal(Event):
    """A strategy-emitted order intent. Its ``signal_id`` is the correctness key."""

    strategy_id: str
    symbol: str
    seq: int

    @property
    def signal_id(self) -> str:
        """``{strategy_id}:{symbol}:{seq}`` — deterministic, replayable (ADR-0006).

        Delegates to ``SignalId``, the single owner of the format (compose here,
        parse in recovery), so the wire form has exactly one author.
        """
        return SignalId(self.strategy_id, self.symbol, self.seq).render()

    @property
    def event_id(self) -> str:
        return self.signal_id

    @property
    def partition_key(self) -> str:
        return self.symbol


@dataclass(frozen=True, slots=True, kw_only=True)
class PlaceSignal(Signal):
    """Intent to place an order (side, qty, type, TIF; ``price`` for LIMIT)."""

    side: Side
    quantity: Decimal
    order_type: OrderType
    time_in_force: TimeInForce
    price: Decimal | None = None
    post_only: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class CancelSignal(Signal):
    """Intent to cancel the order placed by ``target_signal_id`` (ADR-0026).

    A cancel is itself a fresh, replayable intent: it carries its **own** seq'd
    ``signal_id`` (so a re-emitted cancel dedups like any other signal) plus
    ``target_signal_id`` — the ``signal_id`` the strategy emitted for the order
    it wants gone. The strategy references orders by the id it minted; the
    ``ExecutionManager`` re-derives the target ``cloid`` (ADR-0006), keeping the
    ``cloid`` derivation out of strategy code.
    """

    target_signal_id: str


# --- Raw venue facts (ExecutionReport) --------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionReport(Event):
    """A raw venue fact emitted by an ``Exchange`` adapter (ADR-0015).

    ``reconciliation`` is audit/provenance metadata (ADR-0011 inv 6): ``True``
    on the synthetic replicas the ``Reconciler`` builds from a fetched
    ``VenueOrderView``, ``False`` on venue-pushed reports. Excluded from every
    ``event_id`` so both copies of the same fact collapse under dedup; the
    ``ExecutionManager`` propagates it onto the ``OrderEvent`` it publishes.
    """

    cloid: str
    symbol: str
    reconciliation: bool = False

    @property
    def partition_key(self) -> str:
        return self.symbol


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderStatusReport(ExecutionReport):
    """A raw status fact: the venue reports the order's state (ADR-0025).

    The status half of the report split — the reconciler reads open-orders
    (status) and fill-history (fills) separately (ADR-0011 inv 4). ``status``
    is the venue's adjudication mapped into the saga vocabulary; the key is
    the same single-entry-per-state ``{cloid}:{state}`` as the ``OrderEvent``
    the ``ExecutionManager`` turns it into. ``reason`` is optional venue-supplied
    detail for a negative status (e.g. a ``post_only`` rejection); it is
    metadata, excluded from ``event_id``.
    """

    status: OrderState
    venue_oid: str | None = None
    reason: str | None = None

    @property
    def event_id(self) -> str:
        return f"{self.cloid}:{self.status.value}"


@dataclass(frozen=True, slots=True, kw_only=True)
class FillReport(ExecutionReport):
    """A raw fill fact: the venue reports ``trade_id`` filled ``quantity`` @ ``price``."""

    trade_id: str
    quantity: Decimal
    price: Decimal

    @property
    def event_id(self) -> str:
        return f"{self.cloid}:fill:{self.trade_id}"


# --- Canonical saga transitions (OrderEvent) --------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderEvent(Event):
    """A canonical saga transition published by the ``ExecutionManager``.

    ``reconciliation`` is audit/provenance metadata only (ADR-0011 inv 6); it is
    deliberately excluded from ``event_id`` so synthetic and venue-pushed copies
    of the same transition collapse.
    """

    cloid: str
    strategy_id: str
    signal_id: str
    symbol: str
    venue_oid: str | None = None
    reconciliation: bool = False

    @property
    def state(self) -> OrderState:
        """The saga state this event records entry into. Set per subclass."""
        raise NotImplementedError

    @property
    def event_id(self) -> str:
        return f"{self.cloid}:{self.state.value}"

    @property
    def partition_key(self) -> str:
        return self.symbol


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderPlaced(OrderEvent):
    """Intent recorded, cloid assigned, not yet sent (``PENDING``)."""

    @property
    def state(self) -> OrderState:
        return OrderState.PENDING


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderSubmitted(OrderEvent):
    """Sent to the venue, awaiting resolution (``SUBMITTED``)."""

    @property
    def state(self) -> OrderState:
        return OrderState.SUBMITTED


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderLive(OrderEvent):
    """Accepted by the venue and working (``LIVE``)."""

    @property
    def state(self) -> OrderState:
        return OrderState.LIVE


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderFillEvent(OrderEvent):
    """The fill-family shape: a fill keyed on ``trade_id`` (ADR-0025).

    ``event_id`` is ``{cloid}:fill:{trade_id}`` — a correctness key: a
    redelivered or reconciler-synthesized copy of the same trade collapses to
    one id, so ``cum_qty`` can never double-count.
    """

    trade_id: str
    quantity: Decimal
    price: Decimal
    cum_qty: Decimal

    @property
    def event_id(self) -> str:
        return f"{self.cloid}:fill:{self.trade_id}"


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderPartiallyFilled(OrderFillEvent):
    """Partly filled, remainder working (``PARTIALLY_FILLED``)."""

    @property
    def state(self) -> OrderState:
        return OrderState.PARTIALLY_FILLED


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderFilled(OrderFillEvent):
    """Fully filled (``FILLED``)."""

    @property
    def state(self) -> OrderState:
        return OrderState.FILLED


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderCancelled(OrderEvent):
    """Cancelled with any recorded fills preserved (``CANCELLED``).

    Also the resolution of a ghost-reconciled ``PARTIALLY_FILLED`` order —
    "the venue refused it" is false for an order it partially executed
    (ADR-0010).
    """

    @property
    def state(self) -> OrderState:
        return OrderState.CANCELLED


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderDenied(OrderEvent):
    """Refused by the pre-trade guard, never sent (``DENIED``, ADR-0010)."""

    reason: str

    @property
    def state(self) -> OrderState:
        return OrderState.DENIED


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderRejected(OrderEvent):
    """Sent, and the venue adjudicated and refused it (``REJECTED``, ADR-0010).

    Includes the ghost-reconciled case: a ``LIVE`` order that vanished with no
    fills recorded.
    """

    reason: str

    @property
    def state(self) -> OrderState:
        return OrderState.REJECTED


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderFailed(OrderEvent):
    """Sent (or attempted) with positive proof it never landed (``FAILED``,
    ADR-0010). Never minted on a timeout — only on a proven hard failure."""

    reason: str

    @property
    def state(self) -> OrderState:
        return OrderState.FAILED


# --- Venue truth for one cloid (a query result, not an event) ---------------


@dataclass(frozen=True, slots=True, kw_only=True)
class VenueOrderView:
    """One *successful* venue read for a cloid (``Exchange.fetch_order``).

    Bundles the venue's order record (``status``; ``None`` when the venue
    positively has no record) with that cloid's fill history, so the ADR-0011
    open-orders-plus-fill-history cross-check is one read: a view with no
    status and no fills is proof the order never landed. A read that *failed*
    is never a view — ``fetch_order`` returns ``None`` for that (inv 1:
    an outage must never read as "no record").
    """

    status: OrderStatusReport | None
    fills: tuple[FillReport, ...] = ()

    @property
    def has_record(self) -> bool:
        """Whether the venue knows this cloid at all — the resend gate (ADR-0008)."""
        return self.status is not None or bool(self.fills)


# --- Venue truth for the account (a query result, not an event) --------------


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
