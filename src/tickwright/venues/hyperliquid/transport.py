"""The venue HTTP transport seam — one owner for how bytes reach Hyperliquid.

Both the exchange adapter and the universe fetch POST JSON to the venue; this
module is the one place that knows how (and the one seam tests inject fakes
into). Failures surface as ``OSError`` (``TimeoutError`` included), the
connectivity-guard vocabulary every caller keys on.
"""

from collections.abc import Awaitable, Callable
from typing import Any

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
