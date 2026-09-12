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

from tickwright.domain import Backoff, Clock

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
        self._opened: WsConnection | None = None
        self._stopping = False

    async def start(self) -> None:
        """Open and subscribe the first socket now, and return.

        Both adapters call this at boot, because a *refused* first connect has
        to be an error rather than a retry: inside ``run()`` the same failure
        is paced and gone round again, which is right for a reconnect and wrong
        for a boot (#226, #300). ``OSError`` propagates untouched — the whole
        point is that someone above can see it, and pace a retry on the boot's
        own budget. A socket whose subscribe failed is closed before the raise,
        so that retry never leaves one open behind it.

        The socket it opens is handed to ``run()`` rather than consumed here, so
        a caller that starts and never runs holds an idle subscribed socket that
        ``stop()`` still closes. A ``run()`` without a ``start()`` refuses, so
        no caller can skip the boot by not writing the line.
        """
        connection = await self._connect(self._config.ws_url)
        self._connection = connection
        try:
            await self._subscribe(connection)
        except BaseException:
            # The boot retries a failed start() on its deadline (#300), so a
            # socket that connected but never subscribed must not outlive the
            # attempt that opened it.
            self._connection = None
            await connection.close()
            raise
        self._opened = connection

    async def run(self) -> None:
        """Hold the subscription open until ``stop()``, reconnecting as needed.

        Returns only on a stop — a return is never the venue hanging up, which
        is the property both adapters' supervised tasks rest on (ADR-0024): a
        task that completed on its own would leave the engine ``RUNNING`` with
        nothing arriving.

        Raises ``RuntimeError`` if ``start()`` never handed a socket over. The
        first connect is the boot's, where a refusal is an error. Opened here
        it would be paced and retried instead, and that is #300: a caller that
        skipped ``start()`` ran forever behind a ``RUNNING`` engine, ingesting
        nothing. A session stopped before it started has no boot to refuse and
        returns at once, which the teardown after a faulted boot relies on.
        """
        if self._opened is None and not self._stopping:
            raise RuntimeError("WsSession.run() before start(): the first socket is the boot's")
        backoff = Backoff(
            initial=self._config.reconnect_initial_backoff_seconds,
            maximum=self._config.reconnect_max_backoff_seconds,
        )
        while not self._stopping:
            # A socket ``start()`` already opened and subscribed is consumed as
            # it stands; every later turn of this loop opens its own. Taken
            # rather than read, so a reconnect after the first one cannot pick
            # up the dead socket the boot handed over.
            connection = self._opened
            self._opened = None
            if connection is None:
                try:
                    connection = await self._connect(self._config.ws_url)
                except OSError:
                    # Connect refused/unreachable: pace the retry on the injected
                    # clock, doubling up to the cap (virtual under ManualClock), so
                    # an outage can never turn into a reconnect storm.
                    await backoff.sleep_on(self._clock)
                    continue
                # Held for stop(), which ends run() by closing the socket the
                # consumer is blocked on — the one thing that can unblock a reader.
                self._connection = connection
                await self._subscribe(connection)
            backoff.reset()
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
