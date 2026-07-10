"""``ConflatingIngress`` — the keep-latest-per-symbol buffer between a live WS
reader and the ``EventBus`` (ADR-0023).

A live feed must keep draining its socket (heartbeats, backlog) even while a slow
strategy→execution chain is mid-``publish``. So the reader ``offer``s ticks into
this buffer and a separate ``drain`` coroutine publishes them: under backpressure
the buffer **conflates** — newest tick per symbol wins, each superseded tick
emits one ``feed.lagged`` (ADR-0020), never silently — because ``MarketTick`` is
last-value-wins. This is market-data only and upstream of ``publish`` (both bus
backends see the same conflated stream). Sound per CONTEXT.md **Conflation**.

Lives in the venue package for now: it is the one live feed's ingress. A second
venue is the signal to promote it to a shared home (ADR-0031), not before.
"""

import asyncio

from tickwright.domain import EventBus, MarketTick
from tickwright.observability import NamedEvent, named_event


class ConflatingIngress:
    """A latest-value-per-symbol buffer draining ticks to the bus under backpressure."""

    def __init__(self, *, bus: EventBus) -> None:
        self._bus = bus
        self._pending: dict[str, MarketTick] = {}
        self._wake = asyncio.Event()
        self._closed = False

    def offer(self, tick: MarketTick) -> None:
        """Fold one tick in. Keep-latest-per-symbol: superseding an unpublished
        tick emits one ``feed.lagged`` per drop, never silently (ADR-0023)."""
        dropped = self._pending.pop(tick.symbol, None)
        if dropped is not None:
            named_event(
                NamedEvent.FEED_LAGGED,
                symbol=dropped.symbol,
                dropped_trade_id=dropped.trade_id,
            )
        self._pending[tick.symbol] = tick
        self._wake.set()

    def close(self) -> None:
        """Signal no more ticks; ``drain`` finishes the buffer and returns."""
        self._closed = True
        self._wake.set()

    async def drain(self) -> None:
        """Publish buffered ticks to the bus until closed and empty."""
        while True:
            while self._pending:
                symbol = next(iter(self._pending))
                await self._bus.publish(self._pending.pop(symbol))
            if self._closed:
                return
            # No yield between the emptiness check and clear/wait, so a tick
            # offered in during the last publish cannot slip past unseen.
            self._wake.clear()
            await self._wake.wait()
