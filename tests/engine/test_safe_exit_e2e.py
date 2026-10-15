"""Safe exit end to end (issue #476, ADR-0050): what no single slice could prove.

A strategy exits the ways a strategy can, with a reduce-only order and a cancel
all. The operator stops the engine and runs ``flatten`` for the rest. The same
scenario runs on every store and bus pair. Each life gets a new bus over the same
broker and a new store over the same backing, as a restart would.
"""

import asyncio
import json
from collections.abc import Callable, Sequence
from decimal import Decimal
from pathlib import Path

import pytest
from bus_backends import BUS_BACKENDS, make_bus
from exit_runs import LastingVenue, PriceWaitBlocks, paper_on
from kafka_fakes import FakeKafkaBroker
from store_backends import STORE_BACKEND_PARAMS, PostgresBackend, SQLiteBackend, resolve_backend
from venue_doubles import VenueLink

from tickwright.adapters.clock import ManualClock
from tickwright.adapters.feed import ReplayFeed
from tickwright.adapters.paper import PaperExchange
from tickwright.domain import (
    OPERATOR,
    EventBus,
    Exchange,
    ExternalOrderRef,
    InstrumentSpec,
    MarketTick,
    OrderEvent,
    OrderRef,
    OrderState,
    OrderType,
    PlaceOrder,
    Side,
    SignalEmitter,
    TimeInForce,
    derive_cloid,
)
from tickwright.engine.exit_run import ExitJob, Flatten, OperatorCancelAll
from tickwright.engine.guard import RealGuard
from tickwright.engine.runner import Engine
from tickwright.observability.testing import capture_events

SECOND_NS = 1_000_000_000

_SPECS = {
    symbol: InstrumentSpec(symbol=symbol, sz_decimals=3, max_decimals=6, min_notional=Decimal("10"))
    for symbol in ("BTC", "ETH")
}


class _Exiter:
    """Opens a long on its first tick, then exits as far as a strategy can.

    Once the open fills, it sells ``reduce`` reduce-only. With ``rest`` it also
    rests a buy far below the market, and cancels all its orders on the symbol
    once the buy is live.
    """

    def __init__(
        self,
        emitter: SignalEmitter,
        *,
        symbol: str,
        open_qty: str,
        reduce: str | None,
        rest: bool,
    ) -> None:
        self.strategy_id = emitter.strategy_id
        self.symbol = symbol
        self._emitter = emitter
        self._open_qty = Decimal(open_qty)
        self._reduce = None if reduce is None else Decimal(reduce)
        self._rest = rest
        self._open: str | None = None
        self._reduce_id: str | None = None
        self._rest_id: str | None = None
        self._states: dict[str, set[OrderState]] = {}

    async def on_tick(self, tick: MarketTick) -> None:
        if self._open is None:
            self._open = await self._emitter.place(
                symbol=self.symbol,
                side=Side.BUY,
                quantity=self._open_qty,
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.IOC,
            )

    async def on_order_event(self, event: OrderEvent) -> None:
        seen = self._states.setdefault(event.signal_id, set())
        if event.state in seen:
            return
        seen.add(event.state)
        if event.signal_id == self._open and event.state is OrderState.FILLED:
            if self._reduce is not None:
                self._reduce_id = await self._emitter.place(
                    symbol=self.symbol,
                    side=Side.SELL,
                    quantity=self._reduce,
                    order_type=OrderType.MARKET,
                    time_in_force=TimeInForce.IOC,
                    reduce_only=True,
                )
            if self._rest:
                self._rest_id = await self._emitter.place(
                    symbol=self.symbol,
                    side=Side.BUY,
                    quantity=Decimal("0.01"),
                    order_type=OrderType.LIMIT,
                    time_in_force=TimeInForce.GTC,
                    price=Decimal("30000"),
                )
        if event.signal_id == self._rest_id and event.state is OrderState.LIVE:
            await self._emitter.cancel_all(symbol=self.symbol)

    def opened(self, state: OrderState) -> bool:
        """Whether the opening order has reached ``state``."""
        return self._reached(self._open, state)

    def done(self) -> bool:
        return (
            self._reached(self._open, OrderState.FILLED)
            and (self._reduce is None or self._reached(self._reduce_id, OrderState.FILLED))
            and (not self._rest or self._reached(self._rest_id, OrderState.CANCELLED))
        )

    def _reached(self, signal_id: str | None, state: OrderState) -> bool:
        return signal_id is not None and state in self._states.get(signal_id, set())

    def set_next_seq(self, next_seq: int) -> None:
        self._emitter.set_next_seq(next_seq)

    def snapshot(self) -> bytes:
        return json.dumps({"version": 1}).encode()

    def restore(self, data: bytes) -> None:
        pass


class _Lives:
    """One store backing and one Kafka broker, shared by every life of a test."""

    def __init__(self, tmp_path: Path, store: str, bus: str) -> None:
        self._tmp_path = tmp_path
        self.backend: SQLiteBackend | PostgresBackend = resolve_backend(store, tmp_path / "saga.db")
        self._bus = bus
        self._broker = FakeKafkaBroker()
        # Set for a crash test, so the venue keeps what a dead life sent it.
        self.lasting_venue: LastingVenue | None = None

    def bus(self) -> EventBus:
        return make_bus(self._bus, self._broker)

    def ticks(self, name: str, rows: Sequence[dict[str, str]], *, start_s: int) -> Path:
        """A replay file with one second per row, and one trade per symbol in a row."""
        path = self._tmp_path / f"{name}.jsonl"
        path.write_text(
            "".join(
                json.dumps(
                    {
                        "symbol": symbol,
                        "price": price,
                        "size": "1",
                        "aggressor_side": "buy",
                        "trade_id": f"{name}-{n}-{symbol}",
                        "ts_event": (start_s + n) * SECOND_NS,
                    }
                )
                + "\n"
                for n, row in enumerate(rows)
                for symbol, price in row.items()
            )
        )
        return path

    def engine(
        self,
        ticks: Path,
        *,
        exit_job: ExitJob | None = None,
        clock: ManualClock | None = None,
        link: Callable[[PaperExchange], Exchange] | None = None,
    ) -> tuple[Engine, PaperExchange]:
        """One engine life. ``link`` puts a venue double in front of paper."""
        bus = self.bus()
        clock = clock or ManualClock()
        store = self.backend.open()
        # A crash test closes it by hand, as the death of a process would.
        self.last_store = store
        if self.lasting_venue is None:
            paper = paper_on(store, bus=bus, clock=clock, specs=_SPECS)
        else:
            paper = self.lasting_venue.serve(bus, store)
        engine = Engine(
            bus=bus,
            clock=clock,
            store=store,
            exchange=paper if link is None else link(paper),
            feed=ReplayFeed(path=ticks, bus=bus, clock=clock),
            guard=RealGuard(specs=_SPECS, store=store, clock=clock),
            exit_job=exit_job,
        )
        return engine, paper

    def venue(self) -> tuple[dict[str, Decimal], list[object]]:
        """What the paper venue holds: each non-zero position, and every open order.

        Without a lasting venue, this is paper rebuilt from the store, as after a restart.
        """

        async def read(paper: PaperExchange) -> tuple[dict[str, Decimal], list[object]]:
            positions = await paper.fetch_positions()
            orders = await paper.fetch_open_orders()
            assert isinstance(positions, dict) and isinstance(orders, list)
            return {symbol: size for symbol, size in positions.items() if size != 0}, [*orders]

        with self.backend.open() as store:
            if self.lasting_venue is None:
                paper = paper_on(store, bus=self.bus(), clock=ManualClock(), specs=_SPECS)
                return asyncio.run(read(paper))
            return asyncio.run(read(self.lasting_venue.serve(None, store)))

    def positions(self) -> dict[tuple[str | None, str], Decimal]:
        with self.backend.open() as store:
            return {(row.strategy_id, row.symbol): row.signed_size for row in store.all_positions()}


_STRATEGY_ROW = {"BTC": "50000", "ETH": "3000"}
_FLATTEN_ROWS = [{"BTC": "51000", "ETH": "3100"}, {"BTC": "52000", "ETH": "3200"}]


# What the strategy life leaves: alpha bought 0.03 and sold 0.01, beta bought 0.5.
_AFTER_STRATEGIES = {("alpha", "BTC"): Decimal("0.02"), ("beta", "ETH"): Decimal("0.5")}


def _strategy_engine(
    lives: _Lives, link: Callable[[PaperExchange], Exchange] | None = None
) -> tuple[Engine, Callable[[], bool]]:
    """An engine running alpha and beta, and a check that both have exited."""
    engine, _ = lives.engine(lives.ticks("strategy", [_STRATEGY_ROW] * 3, start_s=1), link=link)
    alpha = _Exiter(
        engine.emitter_for("alpha"), symbol="BTC", open_qty="0.03", reduce="0.01", rest=True
    )
    beta = _Exiter(
        engine.emitter_for("beta"), symbol="ETH", open_qty="0.5", reduce=None, rest=False
    )
    engine.register(alpha, symbols={"BTC"})
    engine.register(beta, symbols={"ETH"})
    return engine, lambda: alpha.done() and beta.done()


async def _until(done: Callable[[], bool], run: asyncio.Task[int]) -> None:
    """Wait for ``done``, or for ``run`` to end first, so a failed boot surfaces."""
    while not done() and not run.done():
        await asyncio.sleep(0)


def _strategies_exit(lives: _Lives) -> None:
    """Run the strategy life until both strategies have exited, then stop it."""
    engine, exited = _strategy_engine(lives)

    async def go() -> int:
        run = asyncio.create_task(engine.run())
        await asyncio.wait_for(_until(exited, run), timeout=5)
        await engine.stop()
        return await run

    assert asyncio.run(go()) == 0


@pytest.mark.parametrize("bus", BUS_BACKENDS)
@pytest.mark.parametrize("store", STORE_BACKEND_PARAMS)
def test_a_strategy_exit_then_flatten_leaves_every_partition_and_the_venue_flat(
    tmp_path: Path, store: str, bus: str
) -> None:
    lives = _Lives(tmp_path, store, bus)

    _strategies_exit(lives)
    assert lives.positions() == _AFTER_STRATEGIES

    engine, _ = lives.engine(
        lives.ticks("flatten", _FLATTEN_ROWS, start_s=10), exit_job=Flatten(feed_ends=True)
    )

    async def go() -> int:
        return await asyncio.wait_for(engine.run(), timeout=5)

    assert asyncio.run(go()) == 0
    assert {key: size for key, size in lives.positions().items() if size != 0} == {}
    assert lives.venue() == ({}, [])


@pytest.mark.parametrize("store", STORE_BACKEND_PARAMS)
def test_an_engine_will_not_start_on_a_store_a_flatten_holds(tmp_path: Path, store: str) -> None:
    lives = _Lives(tmp_path, store, "in_memory")
    _strategies_exit(lives)
    clock = PriceWaitBlocks()
    # A live flatten whose feed never prices a symbol, so it waits and holds the store.
    flatten, _ = lives.engine(
        lives.ticks("silent", [], start_s=10), exit_job=Flatten(feed_ends=False), clock=clock
    )
    refused, _ = _strategy_engine(lives)

    async def go() -> tuple[int, int]:
        run = asyncio.create_task(flatten.run())
        waiting = asyncio.create_task(clock.waiting.wait())
        await asyncio.wait({run, waiting}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
        assert waiting.done(), "the flatten never reached its wait for a price"
        refused_code = await asyncio.wait_for(refused.run(), timeout=5)
        await flatten.stop()
        return refused_code, await asyncio.wait_for(run, timeout=5)

    with capture_events() as logs:
        refused_code, flatten_code = asyncio.run(go())

    assert refused_code == 1
    assert [e for e in logs if e["event"] == "engine.faulted" and "StoreLockHeld" in e["error"]]
    # A stopped flatten has not finished its job (ADR-0060).
    assert flatten_code == 1
    assert lives.positions() == _AFTER_STRATEGIES


@pytest.mark.parametrize("store", STORE_BACKEND_PARAMS)
def test_flatten_will_not_start_on_a_store_a_running_engine_holds(
    tmp_path: Path, store: str
) -> None:
    lives = _Lives(tmp_path, store, "in_memory")
    engine, exited = _strategy_engine(lives)
    flatten, _ = lives.engine(
        lives.ticks("flatten", _FLATTEN_ROWS, start_s=10), exit_job=Flatten(feed_ends=True)
    )

    async def go() -> tuple[int, int]:
        run = asyncio.create_task(engine.run())
        await asyncio.wait_for(_until(exited, run), timeout=5)
        refused_code = await asyncio.wait_for(flatten.run(), timeout=5)
        await engine.stop()
        return refused_code, await run

    with capture_events() as logs:
        refused_code, engine_code = asyncio.run(go())

    assert (refused_code, engine_code) == (2, 0)
    assert [e for e in logs if e["event"] == "exit.refused" and e["reason"] == "lock_held"]
    assert lives.positions() == _AFTER_STRATEGIES
    with lives.backend.open() as reader:
        assert reader.load_kill_switch() is None


Send = tuple[str, PlaceOrder | Sequence[OrderRef | ExternalOrderRef]]


class _DiesAtTheSend(VenueLink):
    """The engine dies at the send ``hits`` picks, before it hears back.

    With ``reaches`` the send gets to the venue first. Without it, the send is lost.
    ``hits`` sees every send in order, as ``("place", order)`` or ``("cancel", refs)``.
    """

    def __init__(
        self,
        venue: Exchange,
        cut: Callable[[], None],
        hits: Callable[[Send], bool],
        *,
        reaches: bool,
    ) -> None:
        super().__init__(venue)
        self._cut = cut
        self._hits = hits
        self._reaches = reaches
        self.crashed = asyncio.Event()

    async def place(self, order: PlaceOrder) -> None:
        if not self._hits(("place", order)):
            return await super().place(order)
        self._cut()
        if self._reaches:
            await super().place(order)
        await self._die()

    async def cancel(self, refs: Sequence[OrderRef | ExternalOrderRef]) -> None:
        if not self._hits(("cancel", refs)):
            return await super().cancel(refs)
        self._cut()
        if self._reaches:
            await super().cancel(refs)
        await self._die()

    async def _die(self) -> None:
        self.crashed.set()
        await asyncio.Event().wait()


def _nth(call: str, n: int, *, reduce_only: bool = False) -> Callable[[], Callable[[Send], bool]]:
    """Picks the ``n``th send of ``call``, counting only reduce-only orders if asked."""

    def make() -> Callable[[Send], bool]:
        seen = 0

        def hits(send: Send) -> bool:
            nonlocal seen
            name, arg = send
            if name != call or (reduce_only and not getattr(arg, "reduce_only", False)):
                return False
            seen += 1
            return seen == n

        return hits

    return make


# Each step of the exit path a crash can hit: the life it hits, the send it dies
# at, and whether that send reached the venue.
_CRASHES = {
    "strategy-reduce-only-sell": ("strategy", _nth("place", 1, reduce_only=True), True),
    "strategy-cancel-all-sent": ("strategy", _nth("cancel", 1), True),
    # The order stays resting, so flatten has to pull it before it closes.
    "strategy-cancel-all-lost": ("strategy", _nth("cancel", 1), False),
    "flatten-first-close": ("flatten", _nth("place", 1), True),
    "flatten-second-close": ("flatten", _nth("place", 2), True),
}


def _die_in(lives: _Lives, engine: Engine, dying: _DiesAtTheSend) -> None:
    """Run ``engine`` until it reaches its crash point, then kill the life."""

    async def go() -> None:
        run = asyncio.create_task(engine.run())
        crashed = asyncio.create_task(dying.crashed.wait())
        await asyncio.wait({run, crashed}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
        assert crashed.done(), "the life ended before it reached its crash point"
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)

    with capture_events():
        asyncio.run(go())
    lives.last_store.close()


@pytest.mark.parametrize("crash", list(_CRASHES))
@pytest.mark.parametrize("bus", BUS_BACKENDS)
@pytest.mark.parametrize("store", STORE_BACKEND_PARAMS)
def test_a_crash_at_any_step_then_a_flatten_ends_with_every_partition_and_the_venue_flat(
    tmp_path: Path, store: str, bus: str, crash: str
) -> None:
    lives = _Lives(tmp_path, store, bus)
    venue = lives.lasting_venue = LastingVenue(specs=_SPECS)
    life, hits, reaches = _CRASHES[crash]
    dying = _DiesAtTheSend(venue.paper, venue.cut, hits(), reaches=reaches)

    if life == "strategy":
        engine, _ = _strategy_engine(lives, link=lambda _: dying)
    else:
        _strategies_exit(lives)
        engine, _ = lives.engine(
            lives.ticks("flatten", _FLATTEN_ROWS, start_s=10),
            exit_job=Flatten(feed_ends=True),
            link=lambda _: dying,
        )
    _die_in(lives, engine, dying)

    engine, _ = lives.engine(
        lives.ticks("restart", _FLATTEN_ROWS, start_s=20), exit_job=Flatten(feed_ends=True)
    )

    async def go() -> int:
        return await asyncio.wait_for(engine.run(), timeout=5)

    with capture_events():
        assert asyncio.run(go()) == 0
    assert {key: size for key, size in lives.positions().items() if size != 0} == {}
    assert lives.venue() == ({}, [])


class _Records(VenueLink):
    """Records each order that reaches the venue."""

    def __init__(self, venue: Exchange) -> None:
        super().__init__(venue)
        self.placed: list[PlaceOrder] = []

    async def place(self, order: PlaceOrder) -> None:
        self.placed.append(order)
        await super().place(order)


@pytest.mark.parametrize(
    "job", [Flatten(feed_ends=True), OperatorCancelAll()], ids=["flatten", "cancel-all"]
)
@pytest.mark.parametrize("store", STORE_BACKEND_PARAMS)
def test_an_exit_run_denies_a_strategy_order_kafka_replays_after_a_crash(
    tmp_path: Path, store: str, job: ExitJob
) -> None:
    # Kafka keeps what a dead life never handled. Here that is alpha's third
    # signal, the buy it rests after its reduce-only sell. An exit run hosts
    # no strategy, so it must never send that buy to the venue (issue #476).
    lives = _Lives(tmp_path, store, "kafka")
    venue = lives.lasting_venue = LastingVenue(specs=_SPECS)
    _, hits, reaches = _CRASHES["strategy-reduce-only-sell"]
    dying = _DiesAtTheSend(venue.paper, venue.cut, hits(), reaches=reaches)
    engine, _ = _strategy_engine(lives, link=lambda _: dying)
    _die_in(lives, engine, dying)

    records = _Records(venue.paper)
    engine, _ = lives.engine(
        lives.ticks("restart", _FLATTEN_ROWS, start_s=20), exit_job=job, link=lambda _: records
    )

    async def go() -> int:
        return await asyncio.wait_for(engine.run(), timeout=5)

    with capture_events():
        assert asyncio.run(go()) == 0
    with lives.backend.open() as reader:
        rest_buy = reader.get_order(derive_cloid("alpha:BTC:3"))
        assert rest_buy is not None and rest_buy.state is OrderState.DENIED
        placed = [reader.get_order(order.cloid) for order in records.placed]
    assert {order.strategy_id for order in placed if order is not None} <= {OPERATOR}


def _openers(lives: _Lives, name: str, *, start_s: int) -> tuple[Engine, list[_Exiter]]:
    """An engine whose alpha and beta each open a long on their first tick and stop there."""
    engine, _ = lives.engine(lives.ticks(name, [_STRATEGY_ROW] * 3, start_s=start_s))
    alpha = _Exiter(
        engine.emitter_for("alpha"), symbol="BTC", open_qty="0.03", reduce=None, rest=False
    )
    beta = _Exiter(
        engine.emitter_for("beta"), symbol="ETH", open_qty="0.5", reduce=None, rest=False
    )
    engine.register(alpha, symbols={"BTC"})
    engine.register(beta, symbols={"ETH"})
    return engine, [alpha, beta]


def _run_until_opened(engine: Engine, exiters: list[_Exiter], state: OrderState) -> None:
    """Run until each opening order is denied or filled, then check it reached ``state``."""

    def settled() -> bool:
        return all(e.opened(OrderState.DENIED) or e.opened(OrderState.FILLED) for e in exiters)

    async def go() -> int:
        run = asyncio.create_task(engine.run())
        await asyncio.wait_for(_until(settled, run), timeout=5)
        await engine.stop()
        return await run

    assert asyncio.run(go()) == 0
    assert all(exiter.opened(state) for exiter in exiters)


@pytest.mark.parametrize("bus", BUS_BACKENDS)
@pytest.mark.parametrize("store", STORE_BACKEND_PARAMS)
def test_after_a_flatten_a_restarted_engine_places_nothing_until_the_kill_switch_is_reset(
    tmp_path: Path, store: str, bus: str
) -> None:
    lives = _Lives(tmp_path, store, bus)
    _strategies_exit(lives)
    engine, _ = lives.engine(
        lives.ticks("flatten", _FLATTEN_ROWS, start_s=10), exit_job=Flatten(feed_ends=True)
    )
    assert asyncio.run(asyncio.wait_for(engine.run(), timeout=5)) == 0

    # The operator restarts the strategies without resetting the kill switch.
    engine, exiters = _openers(lives, "halted", start_s=20)
    _run_until_opened(engine, exiters, OrderState.DENIED)
    assert {key: size for key, size in lives.positions().items() if size != 0} == {}
    assert lives.venue() == ({}, [])

    # The operator resets it, as SIGUSR2 does, and the next life trades again.
    with lives.backend.open() as reset:
        RealGuard(specs=_SPECS, store=reset, clock=ManualClock()).reset_kill_switch()
    engine, exiters = _openers(lives, "reset", start_s=30)
    _run_until_opened(engine, exiters, OrderState.FILLED)
    assert {key: size for key, size in lives.positions().items() if size != 0} == {
        ("alpha", "BTC"): Decimal("0.03"),
        ("beta", "ETH"): Decimal("0.5"),
    }
