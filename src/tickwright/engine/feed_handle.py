"""``FeedHandle``: the one way the engine starts the feed, on every run.

A normal run and an exit run both start the feed here, so it starts and stops
in one place (ADR-0024 step 7, ADR-0052).
"""

import asyncio
from collections.abc import Collection

from tickwright.domain import MarketFeed
from tickwright.observability import NamedEvent, named_event


class FeedHandle:
    """The one way the engine starts the feed.

    A normal run starts it with no symbols to add. An exit job starts it for the
    symbols it needs. The engine builds it over its own task group, so the feed
    loop is supervised the same way on both paths.
    """

    def __init__(self, feed: MarketFeed, tasks: asyncio.TaskGroup) -> None:
        self._feed = feed
        self._tasks = tasks
        self.task: asyncio.Task[None] | None = None
        """The feed loop, for the engine's teardown. ``None`` until started."""

    async def start(self, symbols: Collection[str] = ()) -> None:
        """Add ``symbols`` to the feed, connect it, and run its loop."""
        # A feed that cannot add symbols raises on any call, as Hyperliquid does
        # until #475. A normal run adds none, so it must not call at all.
        if symbols:
            self._feed.add_symbols(symbols)
        await self._feed.start()
        named_event(NamedEvent.ENGINE_FEED_STARTED)
        self.task = self._tasks.create_task(self._feed.run())

    async def ended(self) -> None:
        """Wait for the feed loop to return. A feed that fails faults the run instead."""
        if self.task is not None:
            await asyncio.wait([self.task])
