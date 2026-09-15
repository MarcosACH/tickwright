"""The real-client shim: ``websockets``' behavior normalized to the seam.

The reconnect loops of the feed and the funding ingest are specified against the
``WsConnection`` contract — iteration *ends* when the connection dies, and a
failed connect raises ``OSError``. The real ``websockets`` client instead raises
``ConnectionClosed`` from ``recv`` and ``WebSocketException`` from ``connect``;
this suite pins the translation.

The peer is a real ``websockets`` server on the loopback, one level below the
seam, so the real client runs for real. A stub of the client would pin what we
believe the library does. The peer pins what it does.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from http import HTTPStatus

import pytest
from local_peers import silent_peer
from websockets.asyncio.server import Request, Response, ServerConnection, serve

from tickwright.venues.hyperliquid import transport
from tickwright.venues.hyperliquid.transport import WsConnection, open_websocket

type _Handler = Callable[[ServerConnection], Awaitable[None]]
type _Gate = Callable[[ServerConnection, Request], Response | None]


@asynccontextmanager
async def _peer(handler: _Handler, *, gate: _Gate | None = None) -> AsyncIterator[str]:
    """A websocket peer running ``handler`` per connection. Yields its URL."""
    async with serve(handler, "127.0.0.1", 0, process_request=gate) as server:
        port = server.sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}/ws"


async def _drain(connection: WsConnection) -> list[str]:
    return [frame async for frame in connection]


def test_bytes_frames_decode_to_str() -> None:
    async def send_bytes_then_hang_up(peer: ServerConnection) -> None:
        await peer.send(b'{"channel": "pong"}')

    async def scenario() -> list[str]:
        async with _peer(send_bytes_then_hang_up) as url:
            return await _drain(await open_websocket(url))

    # A normal close ends iteration too: the handler returning is the peer
    # closing with 1000, and the frame before it still arrives as ``str``.
    assert asyncio.run(scenario()) == ['{"channel": "pong"}']


def test_an_abnormal_close_ends_iteration_instead_of_raising() -> None:
    async def send_then_drop_the_socket(peer: ServerConnection) -> None:
        await peer.send("frame-1")
        # Wait for the client's ack so the frame is known delivered before the
        # transport is torn down under it with no close frame (1006).
        await peer.recv()
        peer.transport.abort()

    async def scenario() -> list[str]:
        async with _peer(send_then_drop_the_socket) as url:
            connection = await open_websocket(url)
            frames = []
            async for frame in connection:
                frames.append(frame)
                await connection.send("ack")
            return frames

    # The venue hung up mid-stream: the reconnect loop needs the iterator to
    # end, not a ConnectionClosedError tearing the engine down.
    assert asyncio.run(scenario()) == ["frame-1"]


def test_close_ends_a_reader_parked_on_the_socket() -> None:
    async def hold_open(peer: ServerConnection) -> None:
        await peer.wait_closed()

    async def scenario() -> list[str]:
        async with _peer(hold_open) as url:
            connection = await open_websocket(url)
            reader = asyncio.create_task(_drain(connection))
            await asyncio.sleep(0)  # let the reader park on recv()
            await connection.close()
            return await reader

    # How ``stop()`` unblocks a consumer: closing the socket the reader is
    # blocked on ends its iteration, which is the contract ``WsConnection``
    # states and both reconnect loops stand on.
    assert asyncio.run(scenario()) == []


def test_a_denied_handshake_is_an_os_error_for_the_backoff_loop() -> None:
    def deny(peer: ServerConnection, request: Request) -> Response:
        return peer.respond(HTTPStatus.FORBIDDEN, "denied")

    async def never_reached(peer: ServerConnection) -> None:
        raise AssertionError("a denied handshake must not reach the handler")

    async def scenario() -> None:
        async with _peer(never_reached, gate=deny) as url:
            await open_websocket(url)

    # ``websockets`` raises its own ``InvalidStatus`` here, which is not an
    # ``OSError``. The backoff loop only knows ``OSError``.
    with pytest.raises(OSError):
        asyncio.run(scenario())


def test_a_handshake_that_never_completes_is_an_os_error_within_the_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bound on the one call the engine cannot bound for itself.

    ``MarketFeed.start()`` is awaited inline at ADR-0024 step 7 and the runner
    neither retries nor bounds it. The Protocol tells a venue author to put a
    timeout on any blocking call made there; this is the shipped adapter
    obeying its own instruction.

    The real client runs against a real socket that never answers, so the
    ``TimeoutError`` is ``websockets``' own. It reaches the backoff loop only
    because Python 3.11 made ``TimeoutError`` an ``OSError``. That is the fact
    the reconnect loops stand on, pinned here instead of stated in a comment.

    Patching the constant down is also what proves the number is ours: with
    ``open_timeout=`` deleted, ``websockets`` falls back to its own 10s default,
    and this test runs past the wall-clock bound and fails.
    """
    monkeypatch.setattr(transport, "WS_OPEN_TIMEOUT_SECONDS", 0.1)

    async def scenario() -> None:
        async with silent_peer() as port:
            await open_websocket(f"ws://127.0.0.1:{port}/ws")

    started = time.monotonic()
    with pytest.raises(OSError):
        asyncio.run(scenario())
    assert time.monotonic() - started < 5
