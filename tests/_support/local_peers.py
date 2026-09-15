"""Local sockets that stand in for the venue one level below the transport seam.

The transport shims (``venues/hyperliquid/transport.py``) are the one place
``aiohttp`` and ``websockets`` run for real, so their tests cannot fake the
seam they are. They fake the peer instead: a real socket on the loopback that
behaves one chosen way, so the real client runs and the translation to
``OSError`` is what gets pinned.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


@asynccontextmanager
async def silent_peer() -> AsyncIterator[int]:
    """A TCP peer that accepts every connection and never answers.

    Yields the port. The client's own timeout is the only way out, which is
    what makes this the peer for pinning a transport bound.
    """

    async def swallow(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Read until the client gives up, then let go of the socket.
        await reader.read()
        writer.close()

    server = await asyncio.start_server(swallow, "127.0.0.1", 0)
    async with server:
        yield server.sockets[0].getsockname()[1]
