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
from enum import Enum
from typing import ClassVar

from .enums import AggressorSide, OrderState, OrderType, Side, TimeInForce
from .ids import SignalId
from .leverage import LeverageSpec


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


@dataclass(frozen=True, slots=True, kw_only=True)
class MarkTick(Event):
    """A symbol's **mark price**, the Tier-2 valuation input (ADR-0039).

    Market data like ``MarketTick``, and it enters the same way — through the
    ``MarketFeed`` — but it is not a trade: no ``size``, no ``aggressor_side``,
    no trade id, and it is never a fill input. The ``PortfolioProjection`` caches
    the latest one per symbol and recomputes every Tier-2 read from it; no
    ``Strategy`` sees one (ADR-0039), which is why nothing here is a callback.

    **Provenance differs per deployment, compute does not.** Live carries the
    venue's own mark (``activeAssetCtx.ctx.markPx``); paper and replay carry the
    last-trade proxy, derived at the feed so the projection consumes one uniform
    stream everywhere and holds no ``if live:`` for the mark.
    """

    symbol: str
    price: Decimal
    seq: int | None = None
    """The feed's per-symbol source sequence, on a feed that needs one to keep
    two marks apart. ``None`` on the live channel, whose receipt-time
    ``ts_event`` already does; set on ``ReplayFeed``, where a recorded file may
    hold several rows at one ``ts_event``."""

    @property
    def event_id(self) -> str:
        # Weak key (audit/log only, never a correctness key), ADR-0039: a mark is
        # a latest-value, so nothing downstream dedups on this. The replay form
        # carries ``seq`` for the same reason ``MarketTick``'s does — the file's
        # instants are not unique — and the live form has no id to lean on the
        # way ``MarketTick`` leans on the venue's ``tid``, only its own receipt.
        if self.seq is None:
            return f"{self.symbol}:{self.ts_event}"
        return f"{self.symbol}:{self.ts_event}:{self.seq}"

    @property
    def partition_key(self) -> str:
        return self.symbol


# --- Accounting inputs with no carrier ---------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class FundingAccrual(Event):
    """One boundary's periodic cash adjustment on a perp position (ADR-0037).

    A first-class event rather than an internal adjustment, and the *only*
    accounting input that is one: the fee got to be a read-model because it
    rides an existing carrier — the fill — and funding has none. So it becomes
    its own event rather than reinventing durable-keyed idempotent state outside
    the taxonomy built for it (ADR-0025, ADR-0045 §1).

    One shape, both modes: ``PaperExchange`` **generates** it on the ``Clock``
    cadence and ``HyperliquidExchange`` **ingests** the venue's reported payment
    verbatim. ``amount`` is signed and mirrors ``userFunding.usdc`` — ``< 0``
    paid, ``> 0`` received — so live transforms nothing and reconcile is a direct
    field compare. The account applies ``cash += amount``.

    ``boundary_ts_ns`` is the settlement instant the key is built on, distinct
    from ``ts_event``: paper's is the epoch-aligned boundary, live's the venue's
    own ``time``. A given account is either paper or live, so the two never need
    to agree — only to dedupe within one account's stream.
    """

    account_id: str
    symbol: str
    boundary_ts_ns: int
    amount: Decimal

    @property
    def event_id(self) -> str:
        """``{account}:{symbol}:funding:{boundary}`` — ADR-0037's key.

        ``amount`` is deliberately not in it. All three convergence paths can
        re-deliver a boundary already applied — catch-up, live's reconcile
        re-ingest, and a replay rerun — and the last re-derives the amount
        against whatever price proxy that run reached, so keying on the money
        would let a differently-priced re-derivation read as a second payment.
        """
        return f"{self.account_id}:{self.symbol}:funding:{self.boundary_ts_ns}"

    @property
    def partition_key(self) -> str:
        """The symbol, with account identity riding as a *property* of the event.

        This is the resolution ADR-0003's account-scope caveat asked for: the
        one account-qualified event in the taxonomy still orders per symbol,
        because one process trades one account (ADR-0038) — so an account-scoped
        partition would be a single key for the whole stream.
        """
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
    """A raw fill fact: the venue reports ``trade_id`` filled ``quantity`` @ ``price``.

    ``fee`` is what that trade cost, signed — ``> 0`` debited, ``< 0`` a maker
    rebate credited — and settled in USDC, left implicit per ADR-0029's bare-
    ``Decimal`` money convention. The producing ``Exchange`` is its authority:
    paper computes it from the spec's rates at the fill boundary, live reads the
    venue's own reported figure and refuses a fill settled in another token. One
    fee per ``trade_id``, so a redelivered or reconciler-synthesized copy
    collapses under ``event_id`` and can never accrue twice (ADR-0036).

    Defaulted to zero, as ``reconciliation`` above is: a frictionless venue
    charges nothing, and every construction site that predates fees keeps
    reporting exactly what it did.
    """

    trade_id: str
    quantity: Decimal
    price: Decimal
    fee: Decimal = Decimal("0")

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

    ``fee`` is the reporting venue's figure, propagated from the ``FillReport``
    rather than derived here: the ``Exchange`` is its authority, and the same
    dedup key that protects ``cum_qty`` protects it from accruing twice. It is
    **per trade, never cumulative** — unlike ``cum_qty`` beside it — because the
    ledger line it feeds accumulates on the account, so a running total here
    would be summed a second time (ADR-0036).
    """

    trade_id: str
    quantity: Decimal
    price: Decimal
    cum_qty: Decimal
    fee: Decimal = Decimal("0")

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


# --- The account grain's synthetic heal (not an event) ----------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class ReconciliationFill:
    """One Tier-1 size heal: the fill the account cycle books to close a gap
    between the ledger's net and the venue's (ADR-0034).

    **Not a bus event**, deliberately, and the distinction is ADR-0045 §1's: the
    catalog that closes is the *bus* catalog, and this never reaches the bus.
    The account cycle folds it straight into the projection and checkpoints it,
    exactly as ``LedgerChange`` travels — publishing it would put a fact on a
    second channel with no subscriber to read it. It is an ``OrderFillEvent``'s
    peer only where ``Position.apply`` consumes them, which is the whole of what
    "the same idempotent ``apply()`` path" means.

    It is also **not an order**, which is why it shares no base class with one.
    There is no cloid, no signal and no saga behind it: ``clearinghouseState``
    reports positions, never trades, so what the cycle knows is that the account
    holds a size it cannot account for — not which order put it there.

    Two of the differences that follow are the type's whole shape:

    * **``strategy_id`` is fixed at ``None``** — the reserved unattributed
      partition (ADR-0038) — and it is a ``ClassVar`` so no caller can supply
      another. The venue has no per-strategy truth, so attributing foreign flow
      to whichever strategy owns the symbol would corrupt that strategy's PnL
      *and* let its close-my-position logic act on exposure it never opened.
      Making it unconstructable is what turns "per-strategy attribution is never
      reconciled" from a rule a producer honours into one it cannot break.
    * **``side`` is a field**, where ``OrderFillEvent`` deliberately makes it
      ride the saga. That rule exists because an order carries the direction and
      the fill carries the trade; here there is no order, so the direction has
      nowhere else to ride and the quantity stays a magnitude.

    ``fee`` is likewise fixed at zero: the venue's own fees are already inside
    the cash line this heal is correcting *toward*, so charging one here would
    debit them a second time.

    ``event_id`` is stamped with the cycle's own ``ts_ns`` rather than derived
    from the divergence's figures. Content-keying looks more idempotent and is
    the trap: the same drift recurring a month later would collapse onto the
    first heal's id and never be booked at all. Stamping the cycle instead
    collapses a *retried* cycle — the case idempotency is actually for — and
    still books a genuinely new divergence.
    """

    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    ts_ns: int

    strategy_id: ClassVar[None] = None
    fee: ClassVar[Decimal] = Decimal("0")

    @property
    def event_id(self) -> str:
        return f"reconcile:{self.symbol}:{self.ts_ns}"


@dataclass(frozen=True, slots=True, kw_only=True)
class CashCorrection:
    """One Tier-1 cash heal: the figure the account cycle corrects the collateral
    line **to** (ADR-0034, ADR-0042 §4's standing exception).

    ``ReconciliationFill``'s sibling at the account grain, and it is not a fill
    because there is nothing to book: the venue publishes no cash line, so what a
    cycle knows is the balance its snapshot *implies* — never a movement that
    produced it. Modelling it as a signed adjustment would invent a
    ``CashAdjustment`` event ADR-0042 §4 explicitly rejects, and would have to be
    computed against a cash line the same cycle's size heals are still moving.

    So it carries a **target**, and the whole idempotency argument follows from
    that: re-applying it assigns the same figure again. The ``event_id`` is
    provenance rather than a key, stamped with the cycle's ``ts_ns`` for
    ``ReconciliationFill``'s reason — a content key would collapse the same drift
    recurring later onto the first heal's id and never correct it.

    Account grain, so there is no symbol on it: the account has one collateral
    pool, and open PnL is not attributable to it (ADR-0041 §2).
    """

    target: Decimal
    ts_ns: int

    @property
    def event_id(self) -> str:
        return f"reconcile:cash:{self.ts_ns}"


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
