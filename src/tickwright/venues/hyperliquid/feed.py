"""``HyperliquidFeed`` — the live ``MarketFeed``: async WS market-data client.

Connects to the venue WS endpoint directly with an async client — no SDK on
the hot path (ADR-0021) — and subscribes **two public channels per configured
coin**:

* ``trades`` → one ``MarketTick`` per venue trade (ADR-0027 field mapping:
  ``px``/``sz`` → ``Decimal``, ``time`` ms → engine ns, ``tid`` → the live dedup
  key's trade id);
* ``activeAssetCtx`` → one ``MarkTick`` per update, from ``ctx.markPx``
  (ADR-0039) — the venue's own robust-median valuation price, which is what it
  margins and liquidates against.

Both are unauthenticated, so the feed holds no key material at all. The
connection factory is injectable, so the default suite drives recorded frames
through the real parse/publish path with no network.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Any, Protocol

from tickwright.domain import AggressorSide, Backoff, Clock, EventBus, MarketTick, MarkTick
from tickwright.observability import NamedEvent, named_event

from .config import HyperliquidConfig
from .ingress import ConflatingIngress, MarketData
from .reading import UNREADABLE, figure

if TYPE_CHECKING:
    from websockets.asyncio.client import ClientConnection

_NS_PER_MS = 1_000_000
# A dropped frame/row is echoed into its named event for triage; truncated so a
# pathological payload can never bloat the logs.
_MAX_LOGGED_FRAME = 200


class WsConnection(Protocol):
    """The slice of a websocket connection the feed uses (the mockable boundary).

    The contract the reconnect loop stands on: iteration **ends** when the
    connection dies — however it dies — and never raises for it; ``close()``
    also ends iteration (that is how ``stop`` unblocks the reader).
    """

    async def send(self, message: str) -> None: ...

    async def close(self) -> None: ...

    def __aiter__(self) -> AsyncIterator[str]: ...


type Connect = Callable[[str], Awaitable[WsConnection]]
"""Opens a websocket to a URL, raising ``OSError`` on failure. Defaults to the
real client; tests inject fakes."""


async def _open_websocket(url: str) -> WsConnection:
    """The real client, adapted to the seam: a failed connect is an ``OSError``
    (handshake refusals included), so the backoff loop owns every failure."""
    import websockets

    try:
        return _RealWsConnection(await websockets.connect(url))
    except websockets.exceptions.WebSocketException as exc:
        raise ConnectionError(f"hyperliquid websocket connect failed: {exc}") from exc


class _RealWsConnection:
    """Adapts ``websockets``' connection to the ``WsConnection`` contract:
    ``ConnectionClosed`` becomes the end of iteration, frames are always
    ``str``. Constructed only after ``websockets`` is imported."""

    def __init__(self, connection: "ClientConnection") -> None:
        from websockets.exceptions import ConnectionClosed

        self._connection = connection
        self._closed_exc = ConnectionClosed

    async def send(self, message: str) -> None:
        await self._connection.send(message)

    async def close(self) -> None:
        await self._connection.close()

    def __aiter__(self) -> "_RealWsConnection":
        return self

    async def __anext__(self) -> str:
        try:
            message = await self._connection.recv()
        except self._closed_exc:
            raise StopAsyncIteration from None
        return message if isinstance(message, str) else message.decode()


class HyperliquidFeed:
    """The live ``MarketFeed`` adapter for Hyperliquid's two market-data channels."""

    def __init__(
        self,
        *,
        config: HyperliquidConfig,
        bus: EventBus,
        clock: Clock,
        connect: Connect = _open_websocket,
    ) -> None:
        self._config = config
        self._bus = bus
        self._clock = clock
        self._connect = connect
        self._connection: WsConnection | None = None
        self._seq_by_symbol: dict[str, int] = {}
        self._stopping = False

    async def start(self) -> None:
        backoff = Backoff(
            initial=self._config.reconnect_initial_backoff_seconds,
            maximum=self._config.reconnect_max_backoff_seconds,
        )
        while not self._stopping:
            try:
                connection = await self._connect(self._config.ws_url)
            except OSError:
                # Connect refused/unreachable: pace the retry on the injected
                # clock, doubling up to the cap (virtual under ManualClock).
                await backoff.sleep_on(self._clock)
                continue
            backoff.reset()
            self._connection = connection
            await self._subscribe(connection)
            # A fresh ingress per connection: the reader offers ticks, the drain
            # publishes them, conflating under backpressure so a slow subscriber
            # never stalls the socket (ADR-0023). Separate coroutines; either one
            # failing cancels both. A new buffer means no stale tick survives a
            # reconnect.
            ingress = ConflatingIngress(bus=self._bus)
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._read_frames(connection, ingress))
                tg.create_task(ingress.drain())
            # Iteration ended: a stop() is final; anything else was the venue
            # hanging up, so back off once and go resubscribe.
            if self._stopping:
                return
            await backoff.sleep_on(self._clock)

    async def stop(self) -> None:
        self._stopping = True
        if self._connection is not None:
            await self._connection.close()

    async def _read_frames(self, connection: WsConnection, ingress: ConflatingIngress) -> None:
        try:
            async for frame in connection:
                for tick in self._parse(frame):
                    ingress.offer(tick)
                    # One turn of the loop per trade: a keeping-up drain publishes
                    # each tick before the next lands, so conflation only ever
                    # bites under real backpressure (ADR-0023).
                    await asyncio.sleep(0)
        finally:
            ingress.close()

    async def _subscribe(self, connection: WsConnection) -> None:
        """Both public channels, per coin: the trades that make ticks and the
        per-coin context that carries the mark (ADR-0027/0039).

        Neither is authenticated, so the live feed still holds no key material
        at all (ADR-0021) — the mark is public market data exactly as the trade
        is, and reading it off the *account* endpoint instead would have needed
        one.
        """
        for symbol in self._config.symbols:
            for channel in ("trades", "activeAssetCtx"):
                message = {"method": "subscribe", "subscription": {"type": channel, "coin": symbol}}
                await connection.send(json.dumps(message))

    def _parse(self, frame: str) -> list[MarketData]:
        """Parse a WS frame into market data, skipping (and naming) the malformed.

        A corrupt frame or row must never fault the feed: the live stream is
        lossy by contract (ADR-0023), so data we cannot read emits one
        ``feed.frame_dropped`` (ADR-0020) and is skipped — good rows in the same
        batch still flow. Frames from channels this feed does not source are
        *ignored*, not dropped (the venue's ``subscriptionResponse``/``pong``).

        The two channels are shaped differently and the branch is the venue's,
        not a choice: ``trades`` batches rows into a list, ``activeAssetCtx``
        sends one context object per update.
        """
        try:
            message = json.loads(frame)
        except json.JSONDecodeError:
            self._drop_frame(frame)
            return []
        if not isinstance(message, dict):
            return []
        if message.get("channel") == "activeAssetCtx":
            return self._parse_mark(frame, message.get("data"))
        if message.get("channel") != "trades":
            return []
        rows = message.get("data")
        if not isinstance(rows, list):
            self._drop_frame(frame)
            return []
        ticks: list[MarketData] = []
        for row in rows:
            try:
                ticks.append(self._to_tick(row))
            except UNREADABLE:
                self._drop_frame(frame, row)
        return ticks

    def _parse_mark(self, frame: str, data: Any) -> list[MarketData]:
        """One ``activeAssetCtx`` update → one ``MarkTick`` (ADR-0039).

        ``ctx.markPx`` and nothing else in that context: ``oraclePx`` is
        funding's price and ``midPx`` is the book's, both riding the same frame,
        and either would value a position against a number the venue does not
        margin it with. ``allMids`` stays rejected for the same reason.

        The channel carries no timestamp, so ``ts_event`` is receipt time — the
        instant this process learned the mark, which is also the freshness a
        strategy judges against its own clock (ADR-0005).

        No shape check ahead of the read, unlike the ``trades`` arm above: that
        one needs to know whether it has a *list to iterate*, so a malformed
        batch is a different fact from a malformed row in it. Here there is one
        value and one way to fail, and every shape it could fail in — a ``data``
        that is not an object, a missing ``ctx``, a ``ctx`` that is a string —
        already lands in ``UNREADABLE`` as the ``TypeError``/``KeyError`` it is.
        A guard in front would be a second spelling of the same refusal — which
        is why ``data`` is typed as the unparsed body it is: the refusal here is
        the ``except``, not a narrowing.
        """
        try:
            symbol = str(data["coin"])
            price = figure(data["ctx"]["markPx"])
        except UNREADABLE:
            # A mark we cannot stand behind is worse than a trade we cannot: it
            # would not merely mis-publish but propagate into every Tier-2
            # number recomputed from it, surfacing as an ``InvalidOperation``
            # out of some comparison far downstream.
            self._drop_frame(frame, data)
            return []
        now = self._clock.timestamp_ns()
        return [MarkTick(symbol=symbol, price=price, ts_event=now, ts_init=now)]

    def _drop_frame(self, frame: str, row: object = None) -> None:
        """Name one unparseable market-data frame or row and skip it (ADR-0020/0023).

        Both arms reach here — a ``trades`` batch or one of its rows, and an
        ``activeAssetCtx`` context whose mark could not be read — because a
        refusal is one fact however it was shaped.

        **Both** sourced channels are public and unauthenticated (no key
        material anywhere in either payload), so echoing the offending body —
        truncated — is safe and aids triage. That is what licenses the echo, so
        it is stated of the set rather than of one member: a future channel that
        carried key material would have to answer this line before reusing it."""
        named_event(
            NamedEvent.FEED_FRAME_DROPPED,
            frame=frame[:_MAX_LOGGED_FRAME],
            row=None if row is None else repr(row)[:_MAX_LOGGED_FRAME],
        )

    def _to_tick(self, trade: dict[str, object]) -> MarketTick:
        symbol = str(trade["coin"])
        seq = self._seq_by_symbol.get(symbol, 0)
        self._seq_by_symbol[symbol] = seq + 1
        return MarketTick(
            symbol=symbol,
            # ``figure`` is what makes an unreadable ``px``/``sz`` a *dropped row*
            # rather than a tick built on a value we cannot stand behind — a
            # non-finite one that would signal an ``InvalidOperation`` out of some
            # guard's comparison far downstream, or a re-typed one whose digits and
            # scale ``json.loads`` already dropped into a ``float`` before we saw
            # it. It raises into ``UNREADABLE``, which the caller's row guard
            # already catches, so no new control flow is added.
            price=figure(trade["px"]),
            size=figure(trade["sz"]),
            aggressor_side=AggressorSide.BUY if trade["side"] == "B" else AggressorSide.SELL,
            trade_id=str(trade["tid"]),
            seq=seq,
            venue_trade_id=True,  # tid is venue-assigned: dedup on {symbol}:{tid} (ADR-0027)
            ts_event=int(str(trade["time"])) * _NS_PER_MS,
            ts_init=self._clock.timestamp_ns(),
        )
