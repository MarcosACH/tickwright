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

from tickwright.venues.hyperliquid.transport import _RealWsConnection, open_websocket


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
    async def refuse(url: str) -> None:
        raise WebSocketException("handshake denied")

    monkeypatch.setattr(websockets, "connect", refuse)
    with pytest.raises(OSError):
        asyncio.run(open_websocket("wss://api.hyperliquid.xyz/ws"))
