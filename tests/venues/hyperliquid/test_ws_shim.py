"""The real-client shim: ``websockets``' behavior normalized to the seam.

The reconnect loops of the feed and the funding ingest are specified against the
``WsConnection`` contract — iteration *ends* when the connection dies, and a
failed connect raises ``OSError``. The real ``websockets`` client instead raises
``ConnectionClosed`` from ``recv`` and ``WebSocketException`` from ``connect``;
this suite pins the translation, with the client stubbed at its boundary.
"""

import asyncio

import pytest
import websockets
from websockets.exceptions import ConnectionClosedError, WebSocketException

from tickwright.venues.hyperliquid.transport import (
    WS_OPEN_TIMEOUT_SECONDS,
    _RealWsConnection,
    open_websocket,
)


class _StubClientConnection:
    """``recv()`` yields queued payloads, then raises the queued exception."""

    def __init__(self, outcomes: list[str | bytes | Exception]) -> None:
        self._outcomes = list(outcomes)
        self.sent: list[str] = []
        self.closed = False

    async def recv(self) -> str | bytes:
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        self.closed = True


def test_an_abnormal_close_ends_iteration_instead_of_raising() -> None:
    stub = _StubClientConnection(["frame-1", ConnectionClosedError(None, None)])
    connection = _RealWsConnection(stub)  # type: ignore[arg-type]

    async def drain() -> list[str]:
        return [frame async for frame in connection]

    # The venue hung up mid-stream: the reconnect loop needs the iterator to
    # end, not a ConnectionClosedError tearing the engine down.
    assert asyncio.run(drain()) == ["frame-1"]


def test_bytes_frames_decode_to_str() -> None:
    stub = _StubClientConnection([b'{"channel": "pong"}', ConnectionClosedError(None, None)])
    connection = _RealWsConnection(stub)  # type: ignore[arg-type]

    async def drain() -> list[str]:
        return [frame async for frame in connection]

    assert asyncio.run(drain()) == ['{"channel": "pong"}']


def test_a_failed_handshake_is_an_os_error_for_the_backoff_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def refuse(url: str, **options: object) -> None:
        raise WebSocketException("handshake denied")

    monkeypatch.setattr(websockets, "connect", refuse)
    with pytest.raises(OSError):
        asyncio.run(open_websocket("wss://api.hyperliquid.xyz/ws"))


def test_the_handshake_is_bounded_by_a_timeout_this_repo_chose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bound on the one call the engine cannot bound for itself.

    ``MarketFeed.start()`` is awaited inline at ADR-0024 step 7 and the runner
    neither retries nor bounds it — and the task that watches SIGINT is created
    on the line after, so a handshake that never completes has no operator
    escape but SIGKILL. The Protocol says as much and tells a venue author to
    put a timeout on any blocking call made there; this is the shipped adapter
    obeying its own instruction.

    Asserting the argument rather than the elapsed time is the point. ``websockets``
    has defaulted ``open_timeout`` to 10s for several major versions, so a handshake
    *is* bounded today — by a dependency's opinion, which a bump can move without a
    line of ours changing. What must hold is that the number is ours, exactly as
    ``post_json`` states its own ``ClientTimeout`` rather than taking aiohttp's.
    """
    passed: dict[str, object] = {}

    async def capture(url: str, **options: object) -> _StubClientConnection:
        passed.update(options)
        return _StubClientConnection([])

    monkeypatch.setattr(websockets, "connect", capture)
    asyncio.run(open_websocket("wss://api.hyperliquid.xyz/ws"))

    assert passed["open_timeout"] == WS_OPEN_TIMEOUT_SECONDS
