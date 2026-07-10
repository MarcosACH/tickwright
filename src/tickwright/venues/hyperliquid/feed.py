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
from typing import Protocol

from tickwright.domain import AggressorSide, Clock, EventBus, MarketTick
from tickwright.observability import NamedEvent, named_event

from .config import HyperliquidConfig

_NS_PER_MS = 1_000_000


class WsConnection(Protocol):
    """The slice of a websocket connection the feed uses (the mockable boundary)."""

    async def send(self, message: str) -> None: ...

    async def close(self) -> None: ...

    def __aiter__(self) -> AsyncIterator[str]: ...


type Connect = Callable[[str], Awaitable[WsConnection]]
"""Opens a websocket to a URL. Defaults to the real client; tests inject fakes."""


async def _open_websocket(url: str) -> WsConnection:
    import websockets

    return await websockets.connect(url)  # type: ignore[return-value]


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

    async def start(self) -> None:
        connection = await self._connect(self._config.ws_url)
        self._connection = connection
        await self._subscribe(connection)
        # Reader and publisher are separate coroutines so a slow subscriber
        # never stalls the socket (ADR-0023); either one failing cancels both.
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._read_frames(connection))
            tg.create_task(self._publish_conflated())

    async def stop(self) -> None:
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
