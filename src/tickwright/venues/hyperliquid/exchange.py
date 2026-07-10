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
    quantize_price,
)

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
        response = await self._send_action(action)
        await self._report_placement(order, response)

    async def _report_placement(self, order: PlaceOrder, response: object) -> None:
        """Translate the venue's placement adjudication into raw facts on the
        bus (ADR-0015): one order in, one status out of ``statuses``."""
        (status,) = _placement_statuses(response)
        if "resting" in status:
            await self._bus.publish(
                self._status_report(order, OrderState.LIVE, venue_oid=str(status["resting"]["oid"]))
            )
        elif "error" in status:
            # Venue-adjudicated refusal: REJECTED, never DENIED (ADR-0010) —
            # the order was sent and judged, and the venue's reason rides along.
            await self._bus.publish(
                self._status_report(order, OrderState.REJECTED, reason=str(status["error"]))
            )
        elif "filled" in status:
            # The placement response carries no trade ids, and a synthetic one
            # would double-count against reconciliation's venue-tid fills under
            # {cloid}:fill:{tid} dedup — so fetch the venue's own fill records
            # and emit those.
            for report in await self._fetch_fills(order, oid=int(status["filled"]["oid"])):
                await self._bus.publish(report)

    async def _fetch_fills(self, order: PlaceOrder, *, oid: int) -> list[FillReport]:
        """This order's fills from the venue's fill history, by its oid — the
        one id fills carry (they have no cloid on the wire)."""
        entries = await self._info({"type": "userFills", "user": self._user_address})
        if not isinstance(entries, list):
            raise ValueError(f"unrecognized Hyperliquid userFills response: {entries!r}")
        return [
            FillReport(
                ts_event=int(entry["time"]) * _NS_PER_MS,
                ts_init=self._clock.timestamp_ns(),
                cloid=order.cloid,
                symbol=order.symbol,
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
        order: PlaceOrder,
        status: OrderState,
        *,
        venue_oid: str | None = None,
        reason: str | None = None,
    ) -> OrderStatusReport:
        now = self._clock.timestamp_ns()
        return OrderStatusReport(
            ts_event=now,
            ts_init=now,
            cloid=order.cloid,
            symbol=order.symbol,
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


def _placement_statuses(response: object) -> list[dict[str, Any]]:
    """The ``statuses`` array out of a placement response, or a readable error
    for a shape the venue never documented (fail fast on our own parsing)."""
    match response:
        case {"status": "ok", "response": {"data": {"statuses": list(statuses)}}}:
            return statuses
    raise ValueError(f"unrecognized Hyperliquid placement response: {response!r}")


def _wire_decimal(value: Decimal) -> str:
    """Render a ``Decimal`` in the venue's wire format: plain notation, no
    exponent, no trailing zeros (the SDK's ``float_to_wire`` normalization,
    minus the float round-trip)."""
    return f"{value.normalize():f}"
