"""The venue transport seam — one owner for how bytes reach Hyperliquid.

Two shapes of traffic, one module: the exchange adapter and the universe POST
JSON to the venue, while the feed and the funding ingest hold open a websocket.
Both live here because both are consumed by more than one adapter, and neither
belongs to the adapter that happened to need it first. Failures surface as
``OSError`` (``TimeoutError`` included), the connectivity-guard vocabulary every
caller keys on.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from websockets.asyncio.client import ClientConnection

type PostJson = Callable[[str, dict[str, Any]], Awaitable[object]]
"""POSTs ``payload`` as JSON to ``url`` and returns the decoded response,
raising ``OSError`` (``TimeoutError`` included) on transport failure.
Defaults to the real client; tests inject fakes."""


async def post_json(url: str, payload: dict[str, Any]) -> object:
    """The real transport. Imported lazily so the package stays light until a
    live component is actually built (mirrors the feed's websockets import)."""
    import aiohttp

    try:
        # A bounded total per request: a hung venue read must surface as the
        # OSError the connectivity guard keys on, not stall its caller.
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as response:
                response.raise_for_status()
                return await response.json()
    except aiohttp.ClientError as exc:
        # Non-OSError client failures (an HTTP error status, a broken payload)
        # join the one failure vocabulary the connectivity guard keys on.
        raise ConnectionError(f"hyperliquid request to {url} failed: {exc}") from exc


class WsConnection(Protocol):
    """The slice of a websocket connection its callers use (the mockable boundary).

    The contract the reconnect loops stand on: iteration **ends** when the
    connection dies — however it dies — and never raises for it; ``close()``
    also ends iteration (that is how ``stop`` unblocks the reader).
    """

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

It matters most where the reconnect loop is *not* underneath it. ``MarketFeed.start()``
and ``Exchange.start()`` are awaited inline during the boot (ADR-0024 steps 4 and 7),
neither is retried nor bounded by the runner, and the task that watches SIGINT is not
created until after the last of them — so a handshake that never completes wedges a
boot with SIGKILL as the only way out.

The timeout surfaces as ``TimeoutError``, which is an ``OSError`` only because Python
3.11 aliased it — the same load-bearing coincidence ``post_json``'s translation rests
on, asserted for neither transport yet
([#237](https://github.com/MarcosACH/tickwright/issues/237)).
"""


async def open_websocket(url: str) -> WsConnection:
    """The real client, adapted to the seam: a failed connect is an ``OSError``
    (handshake refusals included), so the backoff loop owns every failure."""
    import websockets

    try:
        connection = await websockets.connect(url, open_timeout=WS_OPEN_TIMEOUT_SECONDS)
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
