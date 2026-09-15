"""The real HTTP client shim: ``aiohttp``'s failures normalized to the seam.

Every ``except OSError`` in the venue package keys on ``post_json`` turning a
client failure into the one vocabulary the connectivity guard understands
(ADR-0048). Every other test injects a fake through the ``post`` seam above it,
so this suite is the only place the translation itself runs. The venue is a
real local socket, one level below the seam, so ``aiohttp`` runs for real.
"""

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from aiohttp.typedefs import Handler

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
