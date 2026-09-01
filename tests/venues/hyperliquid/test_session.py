"""``WsSession`` — the reconnect contract both live subscriptions stand on.

The loop these cases drive used to be written twice, once in `feed.py` and once
in `funding.py`, and only the feed's copy was ever driven by a test. What is
asserted here is the sequence itself — pace a refused connect, reset after a
good one, resubscribe every connection, tell a stop from a hangup — so that
neither adapter has to restate it and a third subscription inherits it.

The socket is the process boundary and `FakeWsConnection` is the only thing
mocked; time is virtual throughout, so a doubling backoff costs nothing to
assert.
"""

import asyncio

import pytest
from hyperliquid_fakes import FakeWsConnection, RecordingClock

from tickwright.venues.hyperliquid.config import HyperliquidConfig
from tickwright.venues.hyperliquid.session import WsSession
from tickwright.venues.hyperliquid.transport import WsConnection

CONFIG = HyperliquidConfig(symbols=["BTC"])


class _Driver:
    """A session's two callbacks, recording what each connection was told.

    Stands in for an adapter: ``subscribe`` sends this subscription's one
    message, ``consume`` reads the socket to exhaustion. Both record, because
    what the contract is about is *which connection* got which call.
    """

    def __init__(self) -> None:
        self.subscribed: list[FakeWsConnection] = []
        self.consumed: list[str] = []

    async def subscribe(self, connection: WsConnection) -> None:
        assert isinstance(connection, FakeWsConnection)
        self.subscribed.append(connection)
        await connection.send("subscribe")

    async def consume(self, connection: WsConnection) -> None:
        async for frame in connection:
            self.consumed.append(frame)


def _connector(
    outcomes: list[FakeWsConnection | Exception],
) -> tuple[object, list[str]]:
    """A `Connect` handing back ``outcomes`` in order, recording the URLs asked."""
    asked: list[str] = []

    async def connect(url: str) -> WsConnection:
        asked.append(url)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return connect, asked


def _drive(
    outcomes: list[FakeWsConnection | Exception], *, until_frames: int
) -> tuple[_Driver, RecordingClock, list[str]]:
    """Run a session over ``outcomes`` until ``until_frames`` frames are read,
    then stop it and wait the loop out."""
    driver = _Driver()
    clock = RecordingClock()
    connect, asked = _connector(outcomes)

    async def main() -> None:
        session = WsSession(
            config=CONFIG,
            clock=clock,
            connect=connect,  # type: ignore[arg-type]
            subscribe=driver.subscribe,
            consume=driver.consume,
        )
        running = asyncio.create_task(session.run())
        while len(driver.consumed) < until_frames:
            await asyncio.sleep(0)
        await session.stop()
        await asyncio.wait_for(running, timeout=2)

    asyncio.run(main())
    return driver, clock, asked


def test_a_dropped_socket_reconnects_and_resubscribes_the_new_one() -> None:
    """The venue hangs up mid-run; the session opens another and subscribes it.

    Resubscribing is the half a reconnect is worthless without: a recovered
    socket that nobody subscribed delivers nothing, and the loop would sit
    healthy and silent on it forever.
    """
    first = FakeWsConnection(["frame-1"], drop_when_drained=True)
    second = FakeWsConnection(["frame-2"])

    driver, clock, asked = _drive([first, second], until_frames=2)

    assert driver.consumed == ["frame-1", "frame-2"]
    assert driver.subscribed == [first, second]
    assert first.sent == second.sent == ["subscribe"]
    assert asked == [CONFIG.ws_url, CONFIG.ws_url]
    # One hangup, so one backoff sleep before the reconnect — virtual time only.
    assert clock.sleeps == [1.0]


def test_a_refused_connect_paces_the_retry_and_doubles_until_one_lands() -> None:
    """A venue that is down must not be hammered: the delay doubles from the
    configured initial toward the cap, slept on the injected clock (ADR-0021).
    """
    landed = FakeWsConnection(["frame-1"])

    driver, clock, asked = _drive(
        [ConnectionRefusedError("down"), ConnectionRefusedError("still down"), landed],
        until_frames=1,
    )

    assert driver.consumed == ["frame-1"]
    assert len(asked) == 3
    assert clock.sleeps == [1.0, 2.0]


def test_a_good_connection_resets_the_pacing_so_a_later_outage_starts_over() -> None:
    """The reset is what keeps an hour-long run's third reconnect fast.

    Without it the delay only ever climbs, so a socket that drops once an hour
    would eventually wait the cap out before resubscribing — pacing a healthy
    venue as if it were a dead one.
    """
    first = FakeWsConnection(["frame-1"], drop_when_drained=True)
    second = FakeWsConnection(["frame-2"], drop_when_drained=True)
    third = FakeWsConnection(["frame-3"])

    driver, clock, _ = _drive(
        [first, ConnectionRefusedError("blip"), second, ConnectionRefusedError("blip"), third],
        until_frames=3,
    )

    assert driver.consumed == ["frame-1", "frame-2", "frame-3"]
    # Each connection resets the delay, so the second outage pays 1.0 again
    # rather than continuing to 4.0: hangup, refusal, hangup, refusal.
    assert clock.sleeps == [1.0, 2.0, 1.0, 2.0]


def test_a_stop_ends_the_run_without_reconnecting() -> None:
    """The one way `run()` returns. A stop closing the socket looks exactly like
    the venue hanging up from the consumer's side, so the flag — not the ended
    iteration — is what tells them apart."""
    connection = FakeWsConnection(["frame-1"])
    spare = FakeWsConnection(["frame-2"])

    driver, clock, asked = _drive([connection, spare], until_frames=1)

    assert driver.consumed == ["frame-1"]
    assert len(asked) == 1  # the spare was never opened
    assert clock.sleeps == []  # and nothing was paced on the way out


def test_stopping_a_session_that_never_connected_is_safe() -> None:
    """`Exchange.stop()` and `MarketFeed.stop()` are both documented idempotent
    and safe on something that never started (ADR-0024), and the runner's
    teardown re-walks its steps after a fault — so this is reached for real."""

    async def main() -> None:
        session = WsSession(
            config=CONFIG,
            clock=RecordingClock(),
            connect=_connector([])[0],  # type: ignore[arg-type]
            subscribe=_Driver().subscribe,
            consume=_Driver().consume,
        )
        await session.stop()
        await session.stop()
        # Stopped before it ever ran, the loop must not open a socket at all.
        await asyncio.wait_for(session.run(), timeout=2)

    asyncio.run(main())


def test_a_consumer_that_raises_faults_the_run_rather_than_reconnecting() -> None:
    """The fault channel both adapters are supervised for (ADR-0024/0037).

    The funding ingest refuses a payment it cannot represent by raising, and
    that refusal has to reach the runner. A session that answered it with a
    reconnect would re-read the same contract change forever, on a venue that
    can only ever send it again.
    """
    connection = FakeWsConnection(["frame-1"])
    connect, asked = _connector([connection, FakeWsConnection(["frame-2"])])

    async def consume(connection: WsConnection) -> None:
        async for _ in connection:
            raise RuntimeError("a payment this engine cannot represent")

    async def subscribe(connection: WsConnection) -> None:
        return None

    async def main() -> None:
        session = WsSession(
            config=CONFIG,
            clock=RecordingClock(),
            connect=connect,  # type: ignore[arg-type]
            subscribe=subscribe,
            consume=consume,
        )
        await asyncio.wait_for(session.run(), timeout=2)

    with pytest.raises(RuntimeError, match="cannot represent"):
        asyncio.run(main())
    assert len(asked) == 1
