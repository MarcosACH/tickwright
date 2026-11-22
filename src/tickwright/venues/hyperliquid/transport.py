"""The venue transport seam — one owner for how bytes reach Hyperliquid.

Two shapes of traffic, one module: the exchange adapter and the universe POST
JSON to the venue, while the feed and the funding ingest hold open a websocket.
Both live here because both are consumed by more than one adapter, and neither
belongs to the adapter that happened to need it first. Failures surface as
``OSError`` (``TimeoutError`` included), the connectivity-guard vocabulary every
caller keys on.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from websockets.asyncio.client import ClientConnection
    from websockets.exceptions import ConnectionClosed

type PostJson = Callable[[str, dict[str, Any]], Awaitable[object]]
"""POSTs ``payload`` as JSON to ``url`` and returns the decoded response,
raising ``OSError`` (``TimeoutError`` included) on transport failure, and
``IpRateLimited`` on an HTTP 429. Defaults to the real client; tests inject fakes."""


HTTP_TIMEOUT_SECONDS = 30.0
"""A bounded total per request: a hung venue read must surface as the ``OSError``
the connectivity guard keys on, not stall its caller.

A per-call transport bound and a module constant, for the reasons
``WS_OPEN_TIMEOUT_SECONDS`` gives below. Named rather than inlined so a test can
patch it down and pin the timeout path without waiting the full budget."""


class IpRateLimited(ConnectionError):
    """The venue answered HTTP 429: this IP is over its rate limit (ADR-0056).

    A ``ConnectionError``, so a caller that only keys on ``OSError`` still
    treats it as a failed send. A caller that cares can tell it apart.
    """


async def post_json(url: str, payload: dict[str, Any]) -> object:
    """The real transport. Imported lazily so the package stays light until a
    live component is actually built (mirrors the feed's websockets import)."""
    import aiohttp

    try:
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as response:
                if response.status == 429:
                    raise IpRateLimited(f"hyperliquid request to {url} hit the IP rate limit")
                response.raise_for_status()
                return await response.json()
    except aiohttp.ClientError as exc:
        # Non-OSError client failures (an HTTP error status, a broken payload)
        # join the one failure vocabulary the connectivity guard keys on.
        raise ConnectionError(f"hyperliquid request to {url} failed: {exc}") from exc


@dataclass(frozen=True)
class WsClose:
    """How a websocket ended, so a drop log can say who closed it and why (#686).

    ``closed_by`` is the side that sent the first close frame. ``code`` and
    ``reason`` come from that frame. A socket cut with no close frame at all
    is ``"unknown"``, with no code and no reason.
    """

    closed_by: Literal["venue", "client", "unknown"]
    code: int | None
    reason: str | None


class WsConnection(Protocol):
    """The slice of a websocket connection its callers use (the mockable boundary).

    The contract the reconnect loops stand on: iteration **ends** when the
    connection dies — however it dies — and never raises for it; ``close()``
    also ends iteration (that is how ``stop`` unblocks the reader).
    ``send()`` on a dead connection raises ``ConnectionError``.

    ``close_info`` says how the socket ended. It is ``None`` while the socket
    is open, and set once iteration has ended or a send has failed.
    """

    @property
    def close_info(self) -> WsClose | None: ...

    async def send(self, message: str) -> None: ...

    async def close(self) -> None: ...

    def __aiter__(self) -> AsyncIterator[str]: ...


type Connect = Callable[[str], Awaitable[WsConnection]]
"""Opens a websocket to a URL, raising ``OSError`` on failure. Defaults to the
real client; tests inject fakes."""


WS_OPEN_TIMEOUT_SECONDS = 10.0
"""How long a handshake may take before it becomes the ``OSError`` every caller keys on.

Stated here rather than left to ``websockets`` for the reason ``post_json`` states
its own ``ClientTimeout``: the number belongs to this repo, and a dependency bump
must not be able to move it. The value matches the client's long-standing default,
so this pins today's behaviour rather than changing it.

A per-call transport bound, deliberately a module constant and **not** injected
config. The exchange's ``startup_timeout_seconds`` is a *retry budget spent across
boot guards* (ADR-0044 §6), which is a different thing from a ceiling on one call;
conflating them would give this a knob whose value nothing could reason about.

It matters most where the reconnect loop is *not* underneath it. ``Exchange.start()``
is awaited inline during the boot (ADR-0024 step 4), and ``MarketFeed.start()`` runs at
step 7. Neither is retried nor bounded by the runner. A handshake in ``Exchange.start()``
that never completes holds the boot until the operator stops the engine. Since #614 a
stop cancels the boot, so SIGTERM ends it (``test_boot_stop_e2e.py``). A feed
handshake that never completes leaves the engine running with no data until someone stops it (#604).

The timeout surfaces as ``TimeoutError``, which is an ``OSError`` only because Python
3.11 aliased it — the same load-bearing coincidence ``post_json``'s translation rests
on. Both transports pin it against a real local socket that never answers
(``tests/venues/hyperliquid/test_http_shim.py``, ``test_ws_shim.py``).
"""


WS_PING_INTERVAL_SECONDS = 20.0
"""How often the client sends a websocket-level ping frame.

This is the client's keepalive, not the session's JSON ping that keeps the venue
from closing an idle socket. Pinned for the reason ``WS_OPEN_TIMEOUT_SECONDS``
gives. The value matches the client's default, so behavior does not change.
A test patches it down to make a keepalive timeout fire fast (#686)."""

WS_PING_TIMEOUT_SECONDS = 20.0
"""How long the client waits for a pong before it closes the socket itself.

Such a close is the client's, with code 1011. The drop log says so (#686).
The value matches the client's default, so behavior does not change."""

WS_CLOSE_TIMEOUT_SECONDS = 10.0
"""How long the client waits for the peer to answer its close frame.

After a keepalive timeout the peer is usually gone, so the socket ends only
when this wait runs out. Pinned so a test can shorten it. The value matches the
client's default, so behavior does not change."""


async def open_websocket(url: str) -> WsConnection:
    """The real client, adapted to the seam: a failed connect is an ``OSError``
    (handshake refusals included), so the backoff loop owns every failure."""
    import websockets

    try:
        connection = await websockets.connect(
            url,
            open_timeout=WS_OPEN_TIMEOUT_SECONDS,
            ping_interval=WS_PING_INTERVAL_SECONDS,
            ping_timeout=WS_PING_TIMEOUT_SECONDS,
            close_timeout=WS_CLOSE_TIMEOUT_SECONDS,
        )
        return _RealWsConnection(connection)
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
        self._close_info: WsClose | None = None

    @property
    def close_info(self) -> WsClose | None:
        return self._close_info

    def _record_close(self, exc: "ConnectionClosed") -> None:
        # The side whose close frame came first closed the socket. The other
        # frame, if any, is only the echo of it.
        if exc.rcvd is not None and (exc.sent is None or exc.rcvd_then_sent):
            self._close_info = WsClose(
                closed_by="venue", code=exc.rcvd.code, reason=exc.rcvd.reason
            )
        elif exc.sent is not None:
            # Our own close, such as a keepalive timeout (1011).
            self._close_info = WsClose(
                closed_by="client", code=exc.sent.code, reason=exc.sent.reason
            )
        else:
            self._close_info = WsClose(closed_by="unknown", code=None, reason=None)

    async def send(self, message: str) -> None:
        try:
            await self._connection.send(message)
        except self._closed_exc as exc:
            self._record_close(exc)
            # One type for a dead socket, so the session's ping can tell it
            # apart from a bug (#672).
            raise ConnectionError(f"hyperliquid websocket closed: {exc}") from exc

    async def close(self) -> None:
        await self._connection.close()

    def __aiter__(self) -> "_RealWsConnection":
        return self

    async def __anext__(self) -> str:
        try:
            message = await self._connection.recv()
        except self._closed_exc as exc:
            self._record_close(exc)
            raise StopAsyncIteration from None
        return message if isinstance(message, str) else message.decode()
