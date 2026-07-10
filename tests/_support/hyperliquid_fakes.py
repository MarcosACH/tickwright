"""Fakes for the Hyperliquid process boundaries (the only seams the venue
suite mocks): a recorded-frame WS connection with frame builders shaped like
the venue's ``trades``-channel payloads, and a canned-response HTTP POST for
the exchange/info endpoints."""

import asyncio
import json


class FakeExchangeApi:
    """A canned-response ``post`` seam: records every request, answers in order.

    Shaped like the adapter's transport callable — ``await post(url, payload)``
    — so the whole sign-and-send path runs for real up to the socket. An
    exception instance in ``responses`` is raised instead of returned (the
    transport-failure case)."""

    def __init__(self, responses: list[object]) -> None:
        self.requests: list[tuple[str, dict]] = []
        self._responses = list(responses)

    async def __call__(self, url: str, payload: dict) -> object:
        self.requests.append((url, payload))
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


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
    coin: str, px: str, tid: int, *, side: str = "B", sz: str = "1", time: int = 1_700_000_000_000
) -> dict:
    """One venue ``WsTrade`` payload row."""
    return {"coin": coin, "side": side, "px": px, "sz": sz, "time": time, "tid": tid}
