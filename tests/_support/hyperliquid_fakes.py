"""Fakes for the Hyperliquid WS process boundary (the one seam the venue
suite mocks): a recorded-frame connection and frame builders shaped like the
venue's ``trades``-channel payloads."""

import asyncio
import json


class FakeWsConnection:
    """A recorded-frame WS connection: replays frames, records sends, then idles
    (a live socket delivers nothing between trades) until closed. ``drained``
    fires once every frame has been read *and processed* — it is set when the
    reader comes back for the frame after the last one."""

    def __init__(self, frames: list[str]) -> None:
        self.sent: list[str] = []
        self.drained = asyncio.Event()
        self._frames = list(frames)
        self._closed = asyncio.Event()

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
