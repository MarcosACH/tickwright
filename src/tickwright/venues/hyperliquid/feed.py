"""``HyperliquidFeed`` — the live ``MarketFeed``: async WS ``trades`` client.

Connects to the venue WS endpoint directly with an async client — no SDK on
the hot path (ADR-0021) — subscribes the ``trades`` channel per configured
coin, and publishes each venue trade as a ``MarketTick`` (ADR-0027 field
mapping: ``px``/``sz`` → ``Decimal``, ``time`` ms → engine ns, ``tid`` → the
live dedup key's trade id). The connection factory is injectable, so the
default suite drives recorded frames through the real parse/publish path with
no network.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Protocol

from tickwright.domain import AggressorSide, Clock, EventBus, MarketTick, exact_figure
from tickwright.observability import NamedEvent, named_event

from .backoff import Backoff
from .config import HyperliquidConfig
from .ingress import ConflatingIngress

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
    """The live ``MarketFeed`` adapter for Hyperliquid's ``trades`` channel."""

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
        for symbol in self._config.symbols:
            message = {"method": "subscribe", "subscription": {"type": "trades", "coin": symbol}}
            await connection.send(json.dumps(message))

    def _parse(self, frame: str) -> list[MarketTick]:
        """Parse a WS frame into ticks, skipping (and naming) anything malformed.

        A corrupt frame or trade row must never fault the feed: the live tick
        stream is lossy by contract (ADR-0023), so trades data we cannot read
        emits one ``feed.frame_dropped`` (ADR-0020) and is skipped — good rows in
        the same batch still flow. Frames from other channels are *ignored*, not
        dropped: only the ``trades`` channel is a tick source (like the venue's
        ``subscriptionResponse``/``pong``).
        """
        try:
            message = json.loads(frame)
        except json.JSONDecodeError:
            self._drop_frame(frame)
            return []
        if not isinstance(message, dict) or message.get("channel") != "trades":
            return []
        rows = message.get("data")
        if not isinstance(rows, list):
            self._drop_frame(frame)
            return []
        ticks: list[MarketTick] = []
        for row in rows:
            try:
                ticks.append(self._to_tick(row))
            except (KeyError, ValueError, TypeError, InvalidOperation):
                self._drop_frame(frame, row)
        return ticks

    def _drop_frame(self, frame: str, row: object = None) -> None:
        """Name one unparseable ``trades`` frame or row and skip it (ADR-0020/0023).

        The trades channel is public and unauthenticated (no key material), so
        echoing the offending payload — truncated — is safe and aids triage."""
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
            # ``exact_figure`` is what makes a non-finite ``px``/``sz`` a *dropped
            # row* rather than a ``NaN`` tick: it raises the ``ValueError`` the
            # caller's guard already catches, so no new control flow appears here.
            price=exact_figure(Decimal(str(trade["px"]))),
            size=exact_figure(Decimal(str(trade["sz"]))),
            aggressor_side=AggressorSide.BUY if trade["side"] == "B" else AggressorSide.SELL,
            trade_id=str(trade["tid"]),
            seq=seq,
            venue_trade_id=True,  # tid is venue-assigned: dedup on {symbol}:{tid} (ADR-0027)
            ts_event=int(str(trade["time"])) * _NS_PER_MS,
            ts_init=self._clock.timestamp_ns(),
        )
