"""``HyperliquidExchange`` — the live ``Exchange``: signed placement over
async HTTP.

A thin venue boundary (ADR-0015): translate and send, own no saga. Every
venue quirk stays here, never in the engine (ADR-0030): MARKET becomes an
aggressive IOC limit at ``latest × (1 ± slippage_bound)`` quantized per the
ADR-0017 price rule, ``post_only`` becomes ALO, LIMIT passes through as
GTC/IOC. Signing borrows the SDK's utilities only (ADR-0021) — the HTTP call
is our own async client, and the nonce comes from the injected ``Clock``
(ADR-0005), never the SDK's wall-time helper. The latest price a MARKET is
bounded against is the tick stream's — the adapter subscribes itself, like
every consumer of market data.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

from tickwright.domain import (
    AccountSpec,
    Clock,
    EventBus,
    FillReport,
    InstrumentSpec,
    MarketTick,
    OrderState,
    OrderStatusReport,
    OrderType,
    PlaceOrder,
    Side,
    TimeInForce,
    VenueAccountState,
    VenueOrderView,
    quantize_price,
)
from tickwright.observability import NamedEvent, named_event

from . import transport
from .account import account_spec, normalize_account_state
from .config import HyperliquidConfig
from .preflight import verify_account_mode
from .reading import UNREADABLE, figure
from .transport import PostJson
from .universe import HyperliquidUniverse

_NS_PER_MS = 1_000_000

_TIF_WIRE = {TimeInForce.GTC: "Gtc", TimeInForce.IOC: "Ioc"}

# The saga-terminal states a venue read can resolve to (ADR-0010): once an order
# reaches one, the adapter's placed-order memory for it is dead weight to drop.
_TERMINAL_STATES = frozenset({OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED})


class HyperliquidExchange:
    """The live ``Exchange`` adapter for Hyperliquid perps."""

    def __init__(
        self,
        *,
        config: HyperliquidConfig,
        bus: EventBus,
        clock: Clock,
        universe: HyperliquidUniverse,
        startup_timeout_seconds: float,
        post: PostJson | None = None,
    ) -> None:
        if config.signing_key is None:
            raise ValueError(
                "HyperliquidExchange needs a signing key: set TICKWRIGHT_HYPERLIQUID__SIGNING_KEY"
            )
        # Imported here, not at module top: key material and the SDK's signing
        # stack load only when a live exchange is actually built.
        from eth_account import Account

        self._config = config
        self._bus = bus
        self._clock = clock
        self._universe = universe
        # Late-bound default, exactly as ``fetch_instrument_specs`` resolves its
        # own: on the composition root's arm nothing injects this seam, so a
        # def-time-bound default argument would capture the real client and stay
        # captured — leaving the one HTTP boundary of the built adapter, and so
        # everything ``start()`` reads through it, unreachable from a test.
        self._post = post if post is not None else transport.post_json
        # ADR-0024's barrier budget, handed down rather than minted again
        # (ADR-0044 §6): the boot guards run *before* the barrier, so they
        # cannot be barrier steps, but a boot-time venue read they bound
        # separately would be a second timeout free to disagree with the first.
        self._startup_timeout_seconds = startup_timeout_seconds
        self._wallet = Account.from_key(config.signing_key.get_secret_value())
        # /info queries ask about the account, which is the key's own address
        # unless the key is an API/agent wallet acting for a master account.
        self._user_address = config.account_address or self._wallet.address
        # Read once at composition, like the instrument specs beside it: the
        # account is a deployment fact, not something that moves at runtime.
        self._account_spec = account_spec(config, address=self._user_address)
        self._latest_price: dict[str, Decimal] = {}
        # The last nonce sent: the venue requires per-address nonces to be
        # strictly increasing, and the ms-truncated clock would collide on two
        # sends inside one millisecond — this floor keeps them monotonic.
        self._last_nonce = 0
        # Orders this process placed, by cloid: a cancel needs the symbol (the
        # venue cancels by asset index) and the report needs it back.
        self._placed: dict[str, PlaceOrder] = {}
        # The MARKET slippage bound needs the latest traded price, and the tick
        # stream is where prices live (ADR-0027) — subscribe like any consumer.
        bus.subscribe(MarketTick, self.on_tick)

    async def start(self) -> None:
        """Nothing to connect — placement is request-scoped HTTP and the tick
        subscription is wired at construction — but the venue alignment this
        step exists to host, in order.

        The ``userAbstraction`` mode gate is first and gates everything after it
        (ADR-0046 §3, which opens ADR-0024 step 4): a wrong mode invalidates the
        premise the leverage push's own check reasons from, so reporting
        mismatches computed against a margin model that does not apply would be
        noise on top of an error. The leverage push (ADR-0044 §7) lands behind
        it. Both refusals precede the barrier, so neither can let an order out.
        """
        await verify_account_mode(
            info=self._info,
            address=self._user_address,
            clock=self._clock,
            timeout_seconds=self._startup_timeout_seconds,
        )

    async def stop(self) -> None:
        """Nothing to release: this adapter runs no loop of its own — every
        request it makes is scoped to the call that made it."""
        return None

    async def on_tick(self, tick: MarketTick) -> None:
        self._latest_price[tick.symbol] = tick.price

    async def place(self, order: PlaceOrder) -> None:
        action = {
            "type": "order",
            "orders": [self._order_wire(order)],
            "grouping": "na",
        }
        self._placed[order.cloid] = order
        try:
            response = await self._send_action(action)
            await self._report_placement(order, response)
        except OSError as exc:
            # The send window's truth is unknown — the order may or may not
            # have landed — so there is no fact to report. Name the failure;
            # reconcile-by-cloid resolves the in-flight order (ADR-0008 rule 2).
            self._request_failed("place", order.cloid, exc)

    def _request_failed(self, request: str, cloid: str, exc: OSError) -> None:
        named_event(
            NamedEvent.EXCHANGE_REQUEST_FAILED, request=request, cloid=cloid, error=str(exc)
        )

    def _action_rejected(self, request: str, cloid: str, reason: str) -> None:
        # The venue refused the whole action (bad nonce/signature, an action
        # rate-limit): a 200-OK `err` envelope, not a transport failure and not
        # a per-order REJECTED. Name it and stop — reconcile-by-cloid owns the
        # in-flight order (a place never landed, so a later resend is safe).
        named_event(
            NamedEvent.EXCHANGE_ACTION_REJECTED, request=request, cloid=cloid, reason=reason
        )

    async def cancel(self, cloid: str) -> None:
        try:
            symbol = await self._cancel_symbol(cloid)
            if symbol is None:
                # No usable venue record for this cloid: nothing to cancel,
                # nothing to report — a benign no-op (ADR-0026). An ambiguous
                # read is reconciliation's to resolve, not the adapter's.
                return
            action = {
                "type": "cancelByCloid",
                "cancels": [{"asset": self._universe.asset_indices[symbol], "cloid": cloid}],
            }
            response = await self._send_action(action)
        except OSError as exc:
            # The cancel_requested marker is already durable (ADR-0026), so an
            # ack-lost cancel is reconciliation's to resolve — just name it.
            self._request_failed("cancel", cloid, exc)
            return
        outcome = _action_outcome(response)
        if isinstance(outcome, _ActionError):
            # The cancel action was refused, not adjudicated: a benign no-op —
            # the durable cancel_requested marker leaves it to reconciliation.
            self._action_rejected("cancel", cloid, outcome.message)
            return
        (status,) = outcome
        if status == "success":
            self._placed.pop(cloid, None)  # terminal: drop the placed memory
            await self._bus.publish(
                self._status_report(cloid=cloid, symbol=symbol, status=OrderState.CANCELLED)
            )
        # A per-cancel error means the order is already gone (filled/cancelled/
        # never landed): a benign no-op — the venue's real state arrives as its
        # own report or through reconciliation (ADR-0026).

    async def _cancel_symbol(self, cloid: str) -> str | None:
        """The coin to cancel ``cloid`` under (the venue cancels by asset
        index): this process's own placement, or — after a restart emptied that
        memory — the venue's order record. ``None`` when the venue has no
        usable record (``unknownOid``, or a response we cannot parse):
        reconciliation is the backstop for an ambiguous read (ADR-0026)."""
        order = self._placed.get(cloid)
        if order is not None:
            return order.symbol
        read = _decode_order_status(await self._order_status(cloid))
        return read.coin if isinstance(read, _OrderRecord) else None

    async def fetch_order(self, cloid: str) -> VenueOrderView | None:
        """Venue truth for ``cloid``: the order record plus its fill history,
        the ADR-0011 cross-check in one read. ``unknownOid`` is positive proof
        of no record (an empty view); a read that *failed* is ``None`` — an
        outage must never look like "no record" (inv 1)."""
        try:
            return await self._fetch_view(cloid)
        except OSError:
            # Timeout or transport failure (TimeoutError is an OSError): the
            # read failed, and a failed read is None — the reconciler freezes
            # rather than mistaking an outage for an empty book.
            return None

    async def _fetch_view(self, cloid: str) -> VenueOrderView | None:
        read = _decode_order_status(await self._order_status(cloid))
        if read is _OrderStatusRead.NO_RECORD:
            # unknownOid: a *successful* read that positively has no record —
            # the order never landed (an empty view, not a failed read).
            return VenueOrderView(status=None)
        if read is _OrderStatusRead.UNRECOGNIZED:
            # A shape we cannot parse is a failed read, not a record: freezing
            # (ADR-0011) beats misclassifying venue truth.
            return None
        state = _order_state(read.status)
        if state is None:
            # A status outside the taxonomy is likewise a failed read.
            return None
        # Bound the fills read to this order's own lifetime (ADR-0011): starting
        # at the venue's recorded placement time keeps its fills at the front of
        # the returned window, so a busy account's later fills can never push
        # them past the venue's page cap and silently under-report through the
        # {cloid}:fill:{tid} dedup. The timestamp is the venue's own, so the
        # bound is exact regardless of local clock skew.
        fills = await self._fetch_fills(
            cloid=cloid, symbol=read.coin, oid=read.oid, since_ms=read.timestamp
        )
        if fills is None:
            # An unparseable fills half is a failed read too — freeze, never a
            # partial view that would read as "no fills" (ADR-0011 inv 1).
            return None
        if state in _TERMINAL_STATES:
            # This order is done: drop the placed-order memory a cancel would
            # have used, so the adapter's cache tracks only still-open orders.
            self._placed.pop(cloid, None)
        return VenueOrderView(
            status=self._status_report(
                cloid=cloid, symbol=read.coin, status=state, venue_oid=str(read.oid)
            ),
            fills=tuple(fills),
        )

    async def fetch_account_state(self) -> VenueAccountState | None:
        """Venue truth for the account: one ``clearinghouseState`` read, the
        whole account-and-positions snapshot in one response.

        ``None`` only when the read itself failed — an outage must never read as
        a flat book (ADR-0011 inv 1), exactly as ``fetch_order`` carries it.
        """
        try:
            response = await self._info({"type": "clearinghouseState", "user": self._user_address})
        except OSError:
            # Timeout or transport failure (TimeoutError is an OSError): the read
            # failed, and a failed read is None — the reconciler freezes rather
            # than healing a restored ledger toward a fabricated flat.
            return None
        return normalize_account_state(response)

    async def _order_status(self, cloid: str) -> object:
        """The venue's ``orderStatus`` answer for ``cloid`` — the one read both
        the reconciler's ``fetch_order`` and a post-restart ``cancel`` share."""
        return await self._info({"type": "orderStatus", "user": self._user_address, "oid": cloid})

    async def _report_placement(self, order: PlaceOrder, response: object) -> None:
        """Translate the venue's placement adjudication into raw facts on the
        bus (ADR-0015): one order in, one status out of ``statuses``."""
        outcome = _action_outcome(response)
        if isinstance(outcome, _ActionError):
            # The venue refused the whole action (bad nonce/signature/rate-limit)
            # — the order never entered the book. Drop the placed memory and name
            # it, emitting no terminal: a transient refusal must leave the order
            # resendable, and reconcile-by-cloid resolves it (ADR-0008 rule 2).
            self._placed.pop(order.cloid, None)
            self._action_rejected("place", order.cloid, outcome.message)
            return
        (status,) = outcome
        if "resting" in status:
            await self._bus.publish(
                self._status_report(
                    cloid=order.cloid,
                    symbol=order.symbol,
                    status=OrderState.LIVE,
                    venue_oid=str(status["resting"]["oid"]),
                )
            )
        elif "error" in status:
            # Venue-adjudicated refusal: REJECTED, never DENIED (ADR-0010) —
            # the order was sent and judged, and the venue's reason rides along.
            self._placed.pop(order.cloid, None)  # terminal: drop the placed memory
            await self._bus.publish(
                self._status_report(
                    cloid=order.cloid,
                    symbol=order.symbol,
                    status=OrderState.REJECTED,
                    reason=str(status["error"]),
                )
            )
        elif "filled" in status:
            # The placement response carries no trade ids, and a synthetic one
            # would double-count against reconciliation's venue-tid fills under
            # {cloid}:fill:{tid} dedup — so fetch the venue's own fill records
            # and emit those. This is the read right after placement: the fills
            # are the newest, so the whole-history read cannot miss them. An IOC
            # that filled is terminal, so drop the placed memory regardless of
            # how the fills read resolves.
            self._placed.pop(order.cloid, None)  # terminal: drop the placed memory
            try:
                fills = await self._fetch_fills(
                    cloid=order.cloid, symbol=order.symbol, oid=int(status["filled"]["oid"])
                )
            except OSError as exc:
                # The order filled but reading its fills failed in transport.
                # The placement succeeded, so this is a fills-read failure, not a
                # place failure — name it as such and emit nothing; reconcile's
                # fetch_order re-reads FILLED and heals the fills (ADR-0011).
                self._request_failed("fills", order.cloid, exc)
                return
            # An unparseable (non-transport) fills read is None: emit nothing —
            # reconciliation is the backstop.
            for report in fills or ():
                await self._bus.publish(report)

    async def _fetch_fills(
        self, *, cloid: str, symbol: str, oid: int, since_ms: int | None = None
    ) -> list[FillReport] | None:
        """This order's fills from the venue's fill history, by its oid — the
        one id fills carry (they have no cloid on the wire).

        ``since_ms`` bounds the read to fills at or after a known placement time
        (``userFillsByTime``), so an aged order's fills sit at the front of the
        window rather than risking the venue's page cap; without it the whole
        recent history is read (``userFills``), safe only right after placement
        when this order's fills are the newest. ``None`` on a body we cannot
        parse — a failed read the caller freezes on, never silent truth.
        """
        query: dict[str, Any] = (
            {"type": "userFills", "user": self._user_address}
            if since_ms is None
            else {"type": "userFillsByTime", "user": self._user_address, "startTime": since_ms}
        )
        entries = await self._info(query)
        if not isinstance(entries, list):
            # A shape we cannot parse is a failed read, not "no fills": name it
            # (a venue contract change stays visible) and let the caller freeze
            # or heal, never misread it as truth.
            named_event(
                NamedEvent.EXCHANGE_REQUEST_FAILED,
                request="userFills",
                cloid=cloid,
                error=f"unrecognized fills response: {entries!r}",
            )
            return None
        try:
            return [
                FillReport(
                    ts_event=int(entry["time"]) * _NS_PER_MS,
                    ts_init=self._clock.timestamp_ns(),
                    cloid=cloid,
                    symbol=symbol,
                    trade_id=str(entry["tid"]),
                    # A non-finite figure raises nothing on the way in, so without
                    # ``figure`` a ``NaN`` quantity would ride into a
                    # ``FillReport``, poison ``cum_qty`` by arithmetic and leave
                    # its equality cross-check permanently disagreeing — durably,
                    # since the store round-trips ``"NaN"`` back on recovery. A
                    # re-typed one is the same verdict for a different reason: it
                    # would land a fill priced off a figure ``json.loads`` had
                    # already rounded into a ``float``, no longer provably the
                    # one the venue sent.
                    quantity=figure(entry["sz"]),
                    price=figure(entry["px"]),
                    # Read, never reconstructed (ADR-0036): the venue's figure
                    # already carries this account's volume tier, any referral
                    # discount, and its own 6-dp truncation — none of it
                    # knowable from a schedule here. Its ``crossed`` flag is
                    # baked in for the same reason, which is why the maker/taker
                    # bit is not carried onto the report: on this path there is
                    # nothing left to select with it.
                    fee=_settled_in_usdc(entry),
                )
                for entry in entries
                if entry["oid"] == oid
            ]
        except UNREADABLE as exc:
            # The container was a list but a row inside it is not one we can read
            # — a missing field, a figure that is not a number or not a string,
            # or a row that is not even a mapping. Same verdict as above: a
            # failed read, named and ``None``, never a partial list that reads as
            # the whole truth (ADR-0011 inv 1). ``oid`` is dereferenced by the
            # filter itself, so a malformed row belonging to *another* order is
            # inside this guard too.
            named_event(
                NamedEvent.EXCHANGE_REQUEST_FAILED,
                request="userFills",
                cloid=cloid,
                error=f"{exc!r} in fills response",
            )
            return None

    def instrument_specs(self) -> Mapping[str, InstrumentSpec]:
        """The meta-sourced per-symbol specs (ADR-0031), for the Engine to wire
        into the guard. A copy, so a caller can never mutate the universe."""
        return dict(self._universe.specs)

    def account_spec(self) -> AccountSpec:
        """The venue's static declaration about this process's account, composed
        by ``account.py`` — the one module that knows what qualifies one."""
        return self._account_spec

    async def _info(self, query: dict[str, Any]) -> object:
        return await self._post(f"{self._config.api_url}/info", query)

    def _status_report(
        self,
        *,
        cloid: str,
        symbol: str,
        status: OrderState,
        venue_oid: str | None = None,
        reason: str | None = None,
    ) -> OrderStatusReport:
        now = self._clock.timestamp_ns()
        return OrderStatusReport(
            ts_event=now,
            ts_init=now,
            cloid=cloid,
            symbol=symbol,
            status=status,
            venue_oid=venue_oid,
            reason=reason,
        )

    def _order_wire(self, order: PlaceOrder) -> dict[str, Any]:
        # Field order matters: the venue re-encodes the JSON action with
        # msgpack to verify the signature, so the wire must serialize exactly
        # as it was hashed — a/b/p/s/r/t/c, matching the SDK's encoder.
        return {
            "a": self._universe.asset_indices[order.symbol],
            "b": order.side is Side.BUY,
            "p": _wire_decimal(self._limit_price(order)),
            "s": _wire_decimal(order.quantity),
            "r": False,  # reduce_only is deferred (ADR-0030)
            "t": {"limit": {"tif": self._wire_tif(order)}},
            "c": order.cloid,
        }

    def _limit_price(self, order: PlaceOrder) -> Decimal:
        if order.order_type is OrderType.LIMIT:
            if order.price is None:
                raise ValueError(f"LIMIT order {order.cloid} has no price")
            return order.price
        # MARKET → aggressive IOC limit (ADR-0030): no native market type at
        # the venue, so the order is priced through the book with a bound. The
        # quantizer's passive rounding keeps the bound honest — a buy rounds
        # down, a sell up, never past the slippage cap.
        latest = self._latest_price.get(order.symbol)
        if latest is None:
            raise ValueError(f"no market tick cached for {order.symbol!r}; cannot bound MARKET")
        bound = (
            1 + self._config.slippage_bound
            if order.side is Side.BUY
            else 1 - self._config.slippage_bound
        )
        return quantize_price(latest * bound, order.side, self._universe.specs[order.symbol])

    def _wire_tif(self, order: PlaceOrder) -> str:
        if order.order_type is OrderType.MARKET:
            return "Ioc"
        if order.post_only:
            return "Alo"
        return _TIF_WIRE[order.time_in_force]

    async def _send_action(self, action: dict[str, Any]) -> object:
        from hyperliquid.utils.signing import sign_l1_action

        # The clock's ms, floored to stay strictly above the last nonce: two
        # sends inside one millisecond still get increasing nonces, which the
        # venue requires per address.
        nonce = max(self._clock.timestamp_ns() // _NS_PER_MS, self._last_nonce + 1)
        self._last_nonce = nonce
        signature = sign_l1_action(
            self._wallet, action, None, nonce, None, not self._config.testnet
        )
        payload = {"action": action, "nonce": nonce, "signature": signature}
        return await self._post(f"{self._config.api_url}/exchange", payload)


@dataclass(frozen=True, slots=True)
class _OrderRecord:
    """The venue's decoded ``orderStatus`` order record: the coin it lives
    under, the venue oid, its placement timestamp (ms), and the raw venue status
    string. Each caller applies its own status policy — ``fetch_order`` maps the
    status to saga vocabulary (freezing on one it cannot map) and bounds the
    fills read at ``timestamp``, a post-restart ``cancel`` needs only the coin
    (it cancels by asset index, whatever the status)."""

    coin: str
    oid: int
    timestamp: int
    status: str


class _OrderStatusRead(Enum):
    """An ``orderStatus`` read that yielded no usable order record."""

    NO_RECORD = "no_record"  # unknownOid — the venue positively has no record
    UNRECOGNIZED = "unrecognized"  # a shape we cannot parse — a failed read


def _decode_order_status(response: object) -> _OrderRecord | _OrderStatusRead:
    """Decode a venue ``orderStatus`` response into its order record, or which
    kind of no-record it is: ``NO_RECORD`` for the venue's positive
    ``unknownOid``, ``UNRECOGNIZED`` for any shape outside that and the order
    record (a failed read — never venue truth). The one decode both
    ``fetch_order`` and a post-restart ``cancel`` read the venue through."""
    match response:
        case {"status": "unknownOid"}:
            return _OrderStatusRead.NO_RECORD
        case {
            "status": "order",
            "order": {
                "order": {"coin": str(coin), "oid": int(oid), "timestamp": int(timestamp)},
                "status": str(status),
            },
        }:
            return _OrderRecord(coin=coin, oid=oid, timestamp=timestamp, status=status)
    return _OrderStatusRead.UNRECOGNIZED


def _order_state(status: str) -> OrderState | None:
    """The saga vocabulary for a venue order-status string, or ``None`` for a
    status we cannot map (freeze, never misclassify).

    The venue's taxonomy is a long list of specific causes, but every entry
    resolves by suffix: ``…Rejected`` refusals, ``…Canceled`` / ``…Cancel``
    removals (``canceled``, ``marginCanceled``, ``scheduledCancel``, …).
    """
    match status:
        case "open":
            return OrderState.LIVE
        case "filled":
            return OrderState.FILLED
        case _ if status.endswith(("anceled", "ancel")):
            return OrderState.CANCELLED
        case _ if status.endswith("ejected"):
            return OrderState.REJECTED
    return None


@dataclass(frozen=True, slots=True)
class _ActionError:
    """A Hyperliquid action-level refusal (the ``{"status": "err", ...}``
    envelope): the whole order/cancel action was rejected before adjudication —
    a bad nonce or signature, an action rate-limit — distinct from a per-order
    ``error`` status and never a transport failure. ``message`` is the venue's
    reason string, for the operator's triage."""

    message: str


def _action_outcome(response: object) -> list[Any] | _ActionError:
    """The ``statuses`` array out of an ok /exchange action response (dicts for
    orders, bare strings for cancels), or an ``_ActionError`` for the venue's
    documented action-level ``err`` envelope. A shape that is neither is a
    genuine parse failure we fail fast on (``ValueError``)."""
    match response:
        case {"status": "ok", "response": {"data": {"statuses": list(statuses)}}}:
            return statuses
        case {"status": "err", "response": message}:
            return _ActionError(str(message))
    raise ValueError(f"unrecognized Hyperliquid action response: {response!r}")


def _settled_in_usdc(entry: Mapping[str, Any]) -> Decimal:
    """One fill's reported fee, refusing a fee settled in any other token.

    Money in this engine is a bare ``Decimal`` with USDC left implicit
    (ADR-0029), so a fee denominated in another token has nowhere to go: accruing
    it would add a figure of one currency to a line of another and misstate cash
    with nothing in the ledger recording which token it came from. The assumption
    is guarded here rather than carried as a ``fee_currency`` field nothing yet
    reads — perp fees are USDC-settled today, and spot is out of scope (ADR-0030).

    ``ValueError``, so it joins ``UNREADABLE`` and the caller answers it exactly
    as it answers a missing field: a named failed read and ``None``, never a
    partial list that reads as the whole truth (ADR-0011 inv 1).
    """
    token = entry["feeToken"]
    if token != "USDC":
        raise ValueError(f"fee settled in {token!r}, not USDC")
    return figure(entry["fee"])


def _wire_decimal(value: Decimal) -> str:
    """Render a ``Decimal`` in the venue's wire format: plain notation, no
    exponent, no trailing zeros (the SDK's ``float_to_wire`` normalization,
    minus the float round-trip)."""
    return f"{value.normalize():f}"
