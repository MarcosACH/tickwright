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

from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

from tickwright.domain import (
    Clock,
    EventBus,
    FillReport,
    MarketTick,
    OrderState,
    OrderStatusReport,
    OrderType,
    PlaceOrder,
    Side,
    TimeInForce,
    VenueOrderView,
    quantize_price,
)
from tickwright.observability import NamedEvent, named_event

from .config import HyperliquidConfig
from .universe import HyperliquidUniverse

type PostJson = Callable[[str, dict[str, Any]], Awaitable[object]]
"""POSTs ``payload`` as JSON to ``url`` and returns the decoded response,
raising ``OSError``/``TimeoutError`` on transport failure. Defaults to the
real client; tests inject fakes."""

_NS_PER_MS = 1_000_000

_TIF_WIRE = {TimeInForce.GTC: "Gtc", TimeInForce.IOC: "Ioc"}


async def _post_json(url: str, payload: dict[str, Any]) -> object:
    """The real transport. Imported lazily so the package stays light until a
    live exchange is actually built (mirrors the feed's websockets import)."""
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as response:
            response.raise_for_status()
            return await response.json()


class HyperliquidExchange:
    """The live ``Exchange`` adapter for Hyperliquid perps."""

    def __init__(
        self,
        *,
        config: HyperliquidConfig,
        bus: EventBus,
        clock: Clock,
        universe: HyperliquidUniverse,
        post: PostJson = _post_json,
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
        self._post = post
        self._wallet = Account.from_key(config.signing_key.get_secret_value())
        # /info queries ask about the account, which is the key's own address
        # unless the key is an API/agent wallet acting for a master account.
        self._user_address = config.account_address or self._wallet.address
        self._latest_price: dict[str, Decimal] = {}
        # Orders this process placed, by cloid: a cancel needs the symbol (the
        # venue cancels by asset index) and the report needs it back.
        self._placed: dict[str, PlaceOrder] = {}
        # The MARKET slippage bound needs the latest traded price, and the tick
        # stream is where prices live (ADR-0027) — subscribe like any consumer.
        bus.subscribe(MarketTick, self.on_tick)

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

    async def cancel(self, cloid: str) -> None:
        # The venue cancels by asset index, so the cloid needs a symbol: from
        # this process's own placements, or — after a restart emptied that
        # memory — from the venue's order record itself.
        try:
            order = self._placed.get(cloid)
            symbol = order.symbol if order is not None else await self._resolve_symbol(cloid)
            if symbol is None:
                # The venue positively has no record of this cloid: nothing to
                # cancel, nothing to report — a benign no-op (ADR-0026).
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
        (status,) = _action_statuses(response)
        if status == "success":
            await self._bus.publish(
                self._status_report(cloid=cloid, symbol=symbol, status=OrderState.CANCELLED)
            )
        # A per-cancel error means the order is already gone (filled/cancelled/
        # never landed): a benign no-op — the venue's real state arrives as its
        # own report or through reconciliation (ADR-0026).

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
        response = await self._info(
            {"type": "orderStatus", "user": self._user_address, "oid": cloid}
        )
        match response:
            case {"status": "unknownOid"}:
                return VenueOrderView(status=None)
            case {
                "status": "order",
                "order": {"order": {"coin": str(coin), "oid": int(oid)}, "status": str(status)},
            }:
                state = _order_state(status)
                if state is None:
                    return None
                return VenueOrderView(
                    status=self._status_report(
                        cloid=cloid, symbol=coin, status=state, venue_oid=str(oid)
                    ),
                    fills=tuple(await self._fetch_fills(cloid=cloid, symbol=coin, oid=oid)),
                )
        # A response we cannot map — an unknown shape — is a failed read, not
        # a record: freezing (ADR-0011) beats misclassifying venue truth.
        return None

    async def _resolve_symbol(self, cloid: str) -> str | None:
        """The coin the venue holds ``cloid`` under, or ``None`` if it has no
        record (``unknownOid``)."""
        response = await self._info(
            {"type": "orderStatus", "user": self._user_address, "oid": cloid}
        )
        match response:
            case {"status": "order", "order": {"order": {"coin": str(coin)}}}:
                return coin
        return None

    async def _report_placement(self, order: PlaceOrder, response: object) -> None:
        """Translate the venue's placement adjudication into raw facts on the
        bus (ADR-0015): one order in, one status out of ``statuses``."""
        (status,) = _action_statuses(response)
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
            # and emit those.
            fills = await self._fetch_fills(
                cloid=order.cloid, symbol=order.symbol, oid=int(status["filled"]["oid"])
            )
            for report in fills:
                await self._bus.publish(report)

    async def _fetch_fills(self, *, cloid: str, symbol: str, oid: int) -> list[FillReport]:
        """This order's fills from the venue's fill history, by its oid — the
        one id fills carry (they have no cloid on the wire)."""
        entries = await self._info({"type": "userFills", "user": self._user_address})
        if not isinstance(entries, list):
            raise ValueError(f"unrecognized Hyperliquid userFills response: {entries!r}")
        return [
            FillReport(
                ts_event=int(entry["time"]) * _NS_PER_MS,
                ts_init=self._clock.timestamp_ns(),
                cloid=cloid,
                symbol=symbol,
                trade_id=str(entry["tid"]),
                quantity=Decimal(str(entry["sz"])),
                price=Decimal(str(entry["px"])),
            )
            for entry in entries
            if entry["oid"] == oid
        ]

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

        nonce = self._clock.timestamp_ns() // _NS_PER_MS
        signature = sign_l1_action(
            self._wallet, action, None, nonce, None, not self._config.testnet
        )
        payload = {"action": action, "nonce": nonce, "signature": signature}
        return await self._post(f"{self._config.api_url}/exchange", payload)


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


def _action_statuses(response: object) -> list[Any]:
    """The ``statuses`` array out of an /exchange action response (dicts for
    orders, bare strings for cancels), or a readable error for a shape the
    venue never documented (fail fast on our own parsing)."""
    match response:
        case {"status": "ok", "response": {"data": {"statuses": list(statuses)}}}:
            return statuses
    raise ValueError(f"unrecognized Hyperliquid action response: {response!r}")


def _wire_decimal(value: Decimal) -> str:
    """Render a ``Decimal`` in the venue's wire format: plain notation, no
    exponent, no trailing zeros (the SDK's ``float_to_wire`` normalization,
    minus the float round-trip)."""
    return f"{value.normalize():f}"
