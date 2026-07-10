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
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

from tickwright.domain import AggressorSide, Clock, EventBus, MarketTick
from tickwright.observability import NamedEvent, named_event

from .config import HyperliquidConfig

if TYPE_CHECKING:
    from websockets.asyncio.client import ClientConnection

_NS_PER_MS = 1_000_000


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
        # The ingress conflation buffer (ADR-0023): latest unpublished tick per
        # symbol, drained by the publisher while the reader keeps the WS moving.
        self._pending: dict[str, MarketTick] = {}
        self._wake = asyncio.Event()
        self._reading_done = False
        self._stopping = False

    async def start(self) -> None:
        backoff = self._config.reconnect_initial_backoff_seconds
        while not self._stopping:
            try:
                connection = await self._connect(self._config.ws_url)
            except OSError:
                # Connect refused/unreachable: pace the retry on the injected
                # clock, doubling up to the cap (virtual under ManualClock).
                await self._clock.sleep(backoff)
                backoff = min(backoff * 2, self._config.reconnect_max_backoff_seconds)
                continue
            backoff = self._config.reconnect_initial_backoff_seconds
            self._connection = connection
            self._reading_done = False
            await self._subscribe(connection)
            # Reader and publisher are separate coroutines so a slow subscriber
            # never stalls the socket (ADR-0023); either one failing cancels both.
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._read_frames(connection))
                tg.create_task(self._publish_conflated())
            # Iteration ended: a stop() is final; anything else was the venue
            # hanging up, so back off once and go resubscribe.
            if self._stopping:
                return
            await self._clock.sleep(backoff)
            backoff = min(backoff * 2, self._config.reconnect_max_backoff_seconds)

    async def stop(self) -> None:
        self._stopping = True
        if self._connection is not None:
            await self._connection.close()

    async def _read_frames(self, connection: WsConnection) -> None:
        try:
            async for frame in connection:
                for tick in self._parse(frame):
                    self._conflate_in(tick)
                    # One turn of the loop per trade: a keeping-up publisher
                    # drains each tick before the next lands, so conflation
                    # only ever bites under real backpressure (ADR-0023).
                    await asyncio.sleep(0)
        finally:
            self._reading_done = True
            self._wake.set()

    def _conflate_in(self, tick: MarketTick) -> None:
        """Fold one tick into the buffer. Keep-latest-per-symbol: superseding
        an unpublished tick emits one ``feed.lagged`` per drop, never silently."""
        dropped = self._pending.pop(tick.symbol, None)
        if dropped is not None:
            named_event(
                NamedEvent.FEED_LAGGED,
                symbol=dropped.symbol,
                dropped_trade_id=dropped.trade_id,
            )
        self._pending[tick.symbol] = tick
        self._wake.set()

    async def _publish_conflated(self) -> None:
        while True:
            while self._pending:
                symbol = next(iter(self._pending))
                await self._bus.publish(self._pending.pop(symbol))
            if self._reading_done:
                return
            # No yield between the emptiness check and clear/wait, so a tick
            # conflated in during the last publish cannot slip past unseen.
            self._wake.clear()
            await self._wake.wait()

    async def _subscribe(self, connection: WsConnection) -> None:
        for symbol in self._config.symbols:
            message = {"method": "subscribe", "subscription": {"type": "trades", "coin": symbol}}
            await connection.send(json.dumps(message))

    def _parse(self, frame: str) -> list[MarketTick]:
        message = json.loads(frame)
        if message.get("channel") != "trades":
            return []
        return [self._to_tick(trade) for trade in message["data"]]

    def _to_tick(self, trade: dict[str, object]) -> MarketTick:
        symbol = str(trade["coin"])
        seq = self._seq_by_symbol.get(symbol, 0)
        self._seq_by_symbol[symbol] = seq + 1
        return MarketTick(
            symbol=symbol,
            price=Decimal(str(trade["px"])),
            size=Decimal(str(trade["sz"])),
            aggressor_side=AggressorSide.BUY if trade["side"] == "B" else AggressorSide.SELL,
            trade_id=str(trade["tid"]),
            seq=seq,
            venue_trade_id=True,  # tid is venue-assigned: dedup on {symbol}:{tid} (ADR-0027)
            ts_event=int(str(trade["time"])) * _NS_PER_MS,
            ts_init=self._clock.timestamp_ns(),
        )
