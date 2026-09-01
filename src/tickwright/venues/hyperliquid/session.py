"""``WsSession`` — one venue subscription held open for the life of a run,
across as many sockets as that takes.

Two adapters in this package keep a socket open and they kept the same loop
twice: the feed's public market-data channels (ADR-0021/0023) and the funding
ingest's ``userFundings`` (ADR-0037). Connect, pace a refused connect on the
injected ``Clock``, reset the pacing after a good one, resubscribe, read until
the socket dies, tell a stop from a hangup, and back off before going round —
that sequence is not incidental. It decides whether a ``stop()`` reconnects,
whether a retry storm can hammer the venue, and whether a reconnect resumes or
idles subscribed to nothing, and a copy of it is a second chance to get one of
those wrong. Only the funding copy was ever driven by a test.

What varies between the two is the socket's *use*, so that is what the session
takes: a ``subscribe`` run on every connection (which is what makes a reconnect
resume rather than idle) and a ``consume`` that returns when the connection
dies. Everything else is this module's, once.

**Only a failed connect is answered here.** Anything raised by ``subscribe`` or
``consume`` leaves the session untouched, because both adapters are supervised
in the runner's ``TaskGroup`` (ADR-0024) and that is their fault channel: the
funding ingest's refusal of a payment it cannot represent has to reach it
(ADR-0037), and a session that caught it would reconnect forever against a venue
whose contract had changed.
"""

from collections.abc import Awaitable, Callable

from tickwright.domain import Clock

from .backoff import Backoff
from .config import HyperliquidConfig
from .transport import Connect, WsConnection

type Subscribe = Callable[[WsConnection], Awaitable[None]]
"""Sends this subscription's ``subscribe`` messages on a freshly-opened socket.

Run again on every reconnect, never once at startup: a resubscribe is what makes
a recovered socket carry the same channels the lost one did."""

type Consume = Callable[[WsConnection], Awaitable[None]]
"""Reads the socket until it ends, and owns everything the frames mean.

Returning is how a consumer says the connection is over — however it ended, and
whether or not a ``stop()`` is behind it, which is the session's to tell apart."""


class WsSession:
    """A subscription that survives its sockets: connect, subscribe, consume,
    reconnect, until stopped."""

    def __init__(
        self,
        *,
        config: HyperliquidConfig,
        clock: Clock,
        connect: Connect,
        subscribe: Subscribe,
        consume: Consume,
    ) -> None:
        self._config = config
        self._clock = clock
        self._connect = connect
        self._subscribe = subscribe
        self._consume = consume
        self._connection: WsConnection | None = None
        self._stopping = False

    async def run(self) -> None:
        """Hold the subscription open until ``stop()``, reconnecting as needed.

        Returns only on a stop — a return is never the venue hanging up, which
        is the property both adapters' supervised tasks rest on (ADR-0024): a
        task that completed on its own would leave the engine ``RUNNING`` with
        nothing arriving.
        """
        backoff = Backoff(
            initial=self._config.reconnect_initial_backoff_seconds,
            maximum=self._config.reconnect_max_backoff_seconds,
        )
        while not self._stopping:
            try:
                connection = await self._connect(self._config.ws_url)
            except OSError:
                # Connect refused/unreachable: pace the retry on the injected
                # clock, doubling up to the cap (virtual under ManualClock), so
                # an outage can never turn into a reconnect storm.
                await backoff.sleep_on(self._clock)
                continue
            backoff.reset()
            # Held for stop(), which ends run() by closing the socket the
            # consumer is blocked on — the one thing that can unblock a reader.
            self._connection = connection
            await self._subscribe(connection)
            await self._consume(connection)
            # The consumer is done: a stop() is final; anything else was the
            # venue hanging up, so back off once and go resubscribe.
            if self._stopping:
                return
            await backoff.sleep_on(self._clock)

    async def stop(self) -> None:
        """End the run: refuse further reconnects and close the live socket.

        Safe on a session that never started and on one already stopped — the
        flag is what ``run()`` reads, and there is a socket to close only if a
        connect ever succeeded.

        Returning does **not** mean the consumer has observed the close. That
        gap is the runner's to close and it does, by waiting the supervised task
        out (`Engine._stop_exchange`) or cancelling it (`_stop_feed`); the
        distinction is #277's to settle, and it now has one loop to settle it
        against rather than two.
        """
        self._stopping = True
        if self._connection is not None:
            await self._connection.close()
