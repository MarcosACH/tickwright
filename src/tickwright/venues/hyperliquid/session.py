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

**Only a dead socket is answered here.** That is a failed connect, or a
``ConnectionError`` from a reconnect's ``subscribe`` (#674). Anything else raised by
``subscribe`` or ``consume`` leaves the session untouched, because both adapters are supervised
in the runner's ``TaskGroup`` (ADR-0024) and that is their fault channel: the
funding ingest's refusal of a payment it cannot represent has to reach it
(ADR-0037), and a session that caught it would reconnect forever against a venue
whose contract had changed.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable

from tickwright.domain import Backoff, Clock
from tickwright.observability import NamedEvent, named_event

from .config import HyperliquidConfig
from .transport import Connect, WsClose, WsConnection

PING_INTERVAL_SECONDS = 30
"""How often an open socket sends the venue a ping.

Hyperliquid closes a socket it has had nothing to send on for 60 s. Each ping
makes it answer with a ``pong``, so the socket never goes quiet. The funding
socket hears from the venue only on the hour, so without a ping it was dropped
and resubscribed about once a minute (#672). Source:
https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/timeouts-and-heartbeats

Half the venue's limit leaves room for a late wake-up. A fixed value, not
config: the limit is the venue's, so no operator has a reason to move it.
"""

_PING = json.dumps({"method": "ping"})
_NS_PER_SECOND = 1_000_000_000

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
    reconnect, until stopped.

    ``name`` says which subscription this is in the log, so an operator can
    tell which socket dropped."""

    def __init__(
        self,
        *,
        name: str,
        config: HyperliquidConfig,
        clock: Clock,
        connect: Connect,
        subscribe: Subscribe,
        consume: Consume,
    ) -> None:
        self._name = name
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
        await self._subscribe_or_close(connection)
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
                try:
                    await self._subscribe_or_close(connection)
                except ConnectionError:
                    # The venue closed the socket before the subscribe went out
                    # (#674). That is a hangup, so it is paced like one. The
                    # pacing is not reset, because no socket was subscribed.
                    if self._stopping:
                        return
                    self._log_drop(connection)
                    await backoff.sleep_on(self._clock)
                    continue
            backoff.reset()
            await self._hold(connection)
            # The consumer is done: a stop() is final; anything else was the
            # venue hanging up, so back off once and go resubscribe.
            if self._stopping:
                return
            self._log_drop(connection)
            await backoff.sleep_on(self._clock)

    def _log_drop(self, connection: WsConnection) -> None:
        """Name the drop, with who closed the socket and why (#686).

        A socket that cannot say how it ended logs as ``unknown``. That is
        the honest answer, and it keeps one shape for the event.
        """
        close = connection.close_info or WsClose(closed_by="unknown", code=None, reason=None)
        named_event(
            NamedEvent.VENUE_SOCKET_DROPPED,
            subscription=self._name,
            closed_by=close.closed_by,
            close_code=close.code,
            close_reason=close.reason,
        )

    async def _subscribe_or_close(self, connection: WsConnection) -> None:
        """Subscribe a socket that was just opened, or close it and raise.

        The socket is held for ``stop()`` before the subscribe is sent. A stop
        ends ``run()`` by closing the socket, and that is the one thing that
        unblocks a send or a read.

        A socket that never subscribed is closed here, whatever the error. At
        boot the retry would otherwise leave it open behind it (#300). On a
        reconnect a fault would otherwise leave it for the teardown.
        """
        self._connection = connection
        try:
            await self._subscribe(connection)
        except BaseException:
            self._connection = None
            await connection.close()
            raise

    async def _hold(self, connection: WsConnection) -> None:
        """Consume one connection and ping it, until the consumer is done.

        Returns when the consumer returns. Whichever of the two fails first
        ends both, and the error leaves wrapped in an ``ExceptionGroup``. The
        runner unwraps it (#338). A ping bug therefore faults the run at once.
        It does not wait for the venue to drop the socket it left unpinged.
        """
        async with asyncio.TaskGroup() as group:
            heartbeat = group.create_task(self._ping_until_cancelled(connection))
            try:
                await self._consume(connection)
            finally:
                heartbeat.cancel()

    async def _ping_until_cancelled(self, connection: WsConnection) -> None:
        """Ping the socket every ``PING_INTERVAL_SECONDS`` while it is consumed.

        Waits with ``sleep_until`` rather than ``sleep``. Under ``ManualClock``
        a ``sleep`` advances time itself, so this loop would spin and drag
        virtual time forward on its own.

        A ``ConnectionError`` means the socket died between two reads. That is
        the venue hanging up, and the consumer's ended iteration is what
        answers it with a reconnect. So pinging stops and nothing is raised.
        """
        while True:
            wake_ns = self._clock.timestamp_ns() + PING_INTERVAL_SECONDS * _NS_PER_SECOND
            await self._clock.sleep_until(wake_ns)
            try:
                await connection.send(_PING)
            except ConnectionError:
                return

    async def stop(self) -> None:
        """End the run: refuse further reconnects and close the live socket.

        Safe on a session that never started and on one already stopped — the
        flag is what ``run()`` reads, and there is a socket to close only if a
        connect ever succeeded.

        Returning does **not** mean the consumer has observed the close. That
        gap is the runner's to close, and the seam says how: `stop()` is a
        request, and the cancel and wait behind it are what end `run()`
        (`MarketFeed.run`, `Exchange.run`, #277). Whether `run()` had already
        returned on the close by then is timing, and nothing depends on it.
        """
        self._stopping = True
        if self._connection is not None:
            await self._connection.close()
