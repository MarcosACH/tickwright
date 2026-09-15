"""The real HTTP client shim: ``aiohttp``'s failures normalized to the seam.

Every ``except OSError`` in the venue package keys on ``post_json`` turning a
client failure into the one vocabulary the connectivity guard understands
(ADR-0048). Every other test injects a fake through the ``post`` seam above it,
so this suite is the only place the translation itself runs. The venue is a
real local socket, one level below the seam, so ``aiohttp`` runs for real.
"""

import asyncio
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from aiohttp.typedefs import Handler

from tickwright.venues.hyperliquid import transport
from tickwright.venues.hyperliquid.transport import post_json


async def _post_to(handler: Handler, payload: dict[str, object]) -> object:
    app = web.Application()
    app.router.add_post("/info", handler)
    async with TestServer(app) as server:
        return await post_json(str(server.make_url("/info")), payload)


def test_an_http_error_status_is_a_connection_error_carrying_the_url() -> None:
    async def internal_error(request: web.Request) -> web.Response:
        return web.Response(status=500, text="venue down")

    # An HTTP 500 is a failed read, not an engine fault: it has to arrive as the
    # OSError the guards freeze on, not as aiohttp's own ClientResponseError.
    with pytest.raises(ConnectionError, match="/info"):
        asyncio.run(_post_to(internal_error, {"type": "meta"}))


async def _post_to_a_silent_peer(payload: dict[str, object]) -> object:
    async def swallow(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Accept the connection and read until the client gives up. Never reply.
        await reader.read()
        writer.close()

    server = await asyncio.start_server(swallow, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        return await post_json(f"http://127.0.0.1:{port}/info", payload)


def test_a_venue_that_never_answers_is_an_os_error_within_the_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hung venue read must reach the connectivity guard, not stall its caller.

    The bound is patched down so the test runs in milliseconds. The wall-clock
    check is what makes the constant load-bearing: with the number hardcoded
    elsewhere, the patch would do nothing and this would sit for the full 30s.
    """
    monkeypatch.setattr(transport, "HTTP_TIMEOUT_SECONDS", 0.1)

    started = time.monotonic()
    with pytest.raises(OSError):
        asyncio.run(_post_to_a_silent_peer({"type": "meta"}))
    assert time.monotonic() - started < 5
