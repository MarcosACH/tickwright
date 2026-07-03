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

    @property
    def event_id(self) -> str:
        # Weak key (audit/log only, not a correctness key). Replay form,
        # ADR-0027: a real venue trade id is not assumed on this path.
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
        """``{strategy_id}:{symbol}:{seq}`` — deterministic, replayable (ADR-0006)."""
        return f"{self.strategy_id}:{self.symbol}:{self.seq}"

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


# --- Raw venue facts (ExecutionReport) --------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionReport(Event):
    """A raw venue fact emitted by an ``Exchange`` adapter (ADR-0015)."""

    cloid: str
    symbol: str

    @property
    def partition_key(self) -> str:
        return self.symbol


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
class OrderFilled(OrderEvent):
    """Fully filled (``FILLED``). A fill-family event keyed on ``trade_id``."""

    trade_id: str
    quantity: Decimal
    price: Decimal
    cum_qty: Decimal

    @property
    def state(self) -> OrderState:
        return OrderState.FILLED

    @property
    def event_id(self) -> str:
        return f"{self.cloid}:fill:{self.trade_id}"


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
