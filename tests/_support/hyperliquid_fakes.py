"""Fakes for the Hyperliquid process boundaries (the only seams the venue
suite mocks): a recorded-frame WS connection with frame builders shaped like
the venue's ``trades``-channel payloads, and a canned-response HTTP POST for
the exchange/info endpoints."""

import asyncio
import json

from tickwright.adapters.clock import ManualClock


class RecordingClock(ManualClock):
    """A ``ManualClock`` that also records what it was asked to sleep.

    Every backoff assertion in this package reads ``sleeps`` rather than a wall
    clock, so a retry-pacing test costs no real time. Shared because the pacing
    it measures is now one loop's (``WsSession``) driven from two suites — the
    session's own and the feed's."""

    def __init__(self, start_ns: int = 0) -> None:
        super().__init__(start_ns=start_ns)
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        await super().sleep(seconds)


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
    ``requests`` for wire assertions.

    A routed value may also be a **callable** taking the request payload, for
    the one thing a single response cannot express: an answer that varies by
    *which order* is asked about. The venue's state still does not change
    between reads — ``orderStatus`` is per-oid, so two orders in one reconcile
    pass are two different questions to the same endpoint, not one endpoint
    changing its mind."""

    def __init__(self, responses: dict[str, object]) -> None:
        self.requests: list[tuple[str, dict]] = []
        self._responses = dict(responses)

    async def __call__(self, url: str, payload: dict) -> object:
        self.requests.append((url, payload))
        asked = request_type(url, payload)
        if asked not in self._responses:
            raise AssertionError(
                f"FakeExchangeApi got an unrouted {asked!r} request to {url}; "
                f"routed types: {sorted(self._responses)}"
            )
        response = self._responses[asked]
        if callable(response):
            response = response(payload)
        if isinstance(response, BaseException):
            raise response
        return response


def request_type(url: str, payload: dict) -> str:
    """The venue's discriminator for a request: the action ``type`` for an
    ``/exchange`` post, the query ``type`` for an ``/info`` post.

    Public because routing a request and *asserting* on a recorded one are the
    same question, and one adapter's traffic now carries both kinds — the boot
    gate's ``/info`` read beside the order verbs' signed actions. A caller that
    re-derived the rule from the payload shape would be a second encoding of a
    venue fact, free to drift from the one the fake routes on."""
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


def asset_ctx_frame(coin: str, mark: object, **ctx: object) -> str:
    """One ``activeAssetCtx`` frame, the venue's per-coin context channel.

    ``data`` is a **single object** here, not a list: the venue batches trades
    and does not batch contexts. The extra ``ctx`` members the venue sends
    beside ``markPx`` — ``oraclePx``, ``midPx``, ``funding`` — are carried by
    default and overridable, because the mark being read out of a *populated*
    context is part of what these frames test: ``oraclePx`` is funding's price
    and ``midPx`` is the book's, and neither is the mark.
    """
    return json.dumps(
        {
            "channel": "activeAssetCtx",
            "data": {
                "coin": coin,
                "ctx": {
                    "markPx": mark,
                    "oraclePx": "1",
                    "midPx": "2",
                    "funding": "0.0000125",
                    "openInterest": "1234.5",
                    "prevDayPx": "3",
                    **ctx,
                },
            },
        }
    )


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


def user_fundings_frame(*fundings: dict, snapshot: bool = False) -> str:
    """One ``userFundings`` frame, the venue's per-account payment channel.

    ``data`` wraps a **list**: the first message is the historical snapshot
    (``isSnapshot: true``) and later ones carry the payments on the hour, both
    under the same ``fundings`` key. The engine reads them identically — a
    payment is a payment however it was delivered — so the flag is here only
    because the venue sends it.
    """
    return json.dumps(
        {
            "channel": "userFundings",
            "data": {"isSnapshot": snapshot, "user": "0xuser", "fundings": list(fundings)},
        }
    )
