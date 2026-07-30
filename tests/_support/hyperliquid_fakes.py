"""Fakes for the Hyperliquid process boundaries (the only seams the venue
suite mocks): a recorded-frame WS connection with frame builders shaped like
the venue's ``trades``-channel payloads, and a canned-response HTTP POST for
the exchange/info endpoints."""

import asyncio
import json


class FakeExchangeApi:
    """A venue-state ``post`` seam: answers each request by *what it asks*, not
    by call order.

    Shaped like the adapter's transport callable — ``await post(url, payload)``
    — so the whole sign-and-send path runs for real up to the socket. Responses
    are keyed by the venue's own request discriminator: the action ``type`` for
    ``/exchange`` posts (``order``, ``cancelByCloid``) and the query ``type``
    for ``/info`` posts (``meta``, ``orderStatus``, ``userFills``,
    ``userFillsByTime``, ``clearinghouseState``). One response
    serves every request of that type — the venue's state does not change
    between reads — so a test describes venue state, not a call script. An
    exception value is raised instead of returned (the transport-failure case);
    a request of an unrouted type is a loud failure (the adapter asked
    something the test never set up). Every request is recorded in order in
    ``requests`` for wire assertions."""

    def __init__(self, responses: dict[str, object]) -> None:
        self.requests: list[tuple[str, dict]] = []
        self._responses = dict(responses)

    async def __call__(self, url: str, payload: dict) -> object:
        self.requests.append((url, payload))
        request_type = _request_type(url, payload)
        if request_type not in self._responses:
            raise AssertionError(
                f"FakeExchangeApi got an unrouted {request_type!r} request to {url}; "
                f"routed types: {sorted(self._responses)}"
            )
        response = self._responses[request_type]
        if isinstance(response, BaseException):
            raise response
        return response


def _request_type(url: str, payload: dict) -> str:
    """The venue's discriminator for a request: the action ``type`` for an
    ``/exchange`` post, the query ``type`` for an ``/info`` post."""
    if url.endswith("/exchange"):
        return str(payload["action"]["type"])
    return str(payload["type"])


def resting_response(oid: int) -> dict:
    """The venue's successful placement response for an order that rests."""
    return {
        "status": "ok",
        "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": oid}}]}},
    }


class FakeWsConnection:
    """A recorded-frame WS connection: replays frames, records sends, then idles
    (a live socket delivers nothing between trades) until closed. ``drained``
    fires once every frame has been read *and processed* — it is set when the
    reader comes back for the frame after the last one."""

    def __init__(self, frames: list[str], *, drop_when_drained: bool = False) -> None:
        self.sent: list[str] = []
        self.drained = asyncio.Event()
        self._frames = list(frames)
        self._closed = asyncio.Event()
        self._drop_when_drained = drop_when_drained

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        self._closed.set()

    def __aiter__(self) -> "FakeWsConnection":
        return self

    async def __anext__(self) -> str:
        if self._frames:
            # A real socket read yields to the event loop at least once per
            # frame; without this the reader would starve the publisher.
            await asyncio.sleep(0)
            return self._frames.pop(0)
        self.drained.set()
        if self._drop_when_drained:
            # The venue hung up: iteration ends without a stop() being asked.
            raise StopAsyncIteration
        await self._closed.wait()
        raise StopAsyncIteration


def trades_frame(*trades: dict) -> str:
    """One ``trades``-channel frame carrying ``trades`` (the venue batches)."""
    return json.dumps({"channel": "trades", "data": list(trades)})


def trade(
    coin: str,
    px: object,
    tid: int,
    *,
    side: str = "B",
    sz: object = "1",
    time: int = 1_700_000_000_000,
) -> dict:
    """One venue ``WsTrade`` payload row.

    ``px``/``sz`` are ``object`` rather than ``str`` deliberately: the venue
    reports both as decimal strings, and a test that a *re-typed* figure freezes
    the read has to be able to build the payload the venue would send if that
    contract changed.
    """
    return {"coin": coin, "side": side, "px": px, "sz": sz, "time": time, "tid": tid}
