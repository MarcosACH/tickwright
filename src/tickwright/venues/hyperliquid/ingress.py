"""``ConflatingIngress`` — the keep-latest-per-stream-per-symbol buffer between a
live WS reader and the ``EventBus`` (ADR-0023).

A live feed must keep draining its socket (heartbeats, backlog) even while a slow
strategy→execution chain is mid-``publish``. So the reader ``offer``s market data
into this buffer and a separate ``drain`` coroutine publishes it: under
backpressure the buffer **conflates** — newest value per stream per symbol wins,
each superseded one emits one ``feed.lagged`` (ADR-0020), never silently —
because both ``MarketTick`` and ``MarkTick`` are last-value-wins. This is
market-data only and upstream of ``publish`` (both bus backends see the same
conflated stream). Sound per CONTEXT.md **Conflation**.

The key is ``(stream, symbol)`` rather than the symbol alone, and that is the whole
of what makes the two streams independent: a mark is not a later version of a
trade, so one BTC mark must never swallow the BTC trade queued beside it — the
account would stop filling against a stream that was still arriving — and a busy
symbol's marks must not starve behind its trades.

Lives in the venue package for now: it is the one live feed's ingress. A second
venue is the signal to promote it to a shared home (ADR-0031), not before.
"""

import asyncio

from tickwright.domain import EventBus, MarketTick, MarkTick
from tickwright.observability import NamedEvent, named_event

type MarketData = MarketTick | MarkTick
"""What the live feed sources: the two last-value-wins market-data events.

The **one** spelling of that set — ``HyperliquidFeed`` parses to it and this
buffer conflates it, so a third stream widens this line and the type checker
finds both sides. It sits here rather than in ``feed.py``, which is the producer
and would read as its more natural home, only because the dependency runs
feed → ingress: declaring it there would close a cycle. A shared home comes
with the ingress itself, when a second venue promotes it out of this package
(ADR-0031)."""


class ConflatingIngress:
    """A latest-value buffer draining market data to the bus under backpressure."""

    def __init__(self, *, bus: EventBus) -> None:
        self._bus = bus
        self._pending: dict[tuple[type[MarketData], str], MarketData] = {}
        self._wake = asyncio.Event()
        self._closed = False

    def offer(self, event: MarketData) -> None:
        """Fold one event in. Keep-latest per stream per symbol: superseding an
        unpublished one emits one ``feed.lagged`` per drop, never silently
        (ADR-0023)."""
        key = (type(event), event.symbol)
        dropped = self._pending.pop(key, None)
        if dropped is not None:
            named_event(
                NamedEvent.FEED_LAGGED,
                symbol=dropped.symbol,
                # Only a trade has one, and a mark carries no id of its own — it
                # is a latest-value, so there is nothing to identify but the
                # symbol and the stream it was dropped from. Narrowed rather
                # than read reflectively: ``MarketData`` is a closed union, and
                # the whole point of spelling it once is that adding a third
                # stream makes the type checker visit both sides. A
                # ``getattr(..., None)`` would answer that third stream ``None``
                # and compile, which is the one outcome the alias exists to
                # prevent. #229 owns retiring the field; until then this keeps
                # the checker on it.
                dropped_trade_id=dropped.trade_id if isinstance(dropped, MarketTick) else None,
                stream=type(dropped).__name__,
            )
        self._pending[key] = event
        self._wake.set()

    def close(self) -> None:
        """Signal no more events; ``drain`` finishes the buffer and returns."""
        self._closed = True
        self._wake.set()

    async def drain(self) -> None:
        """Publish buffered events to the bus until closed and empty."""
        while True:
            while self._pending:
                key = next(iter(self._pending))
                await self._bus.publish(self._pending.pop(key))
            if self._closed:
                return
            # No yield between the emptiness check and clear/wait, so an event
            # offered in during the last publish cannot slip past unseen.
            self._wake.clear()
            await self._wake.wait()
