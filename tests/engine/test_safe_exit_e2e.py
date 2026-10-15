"""Safe exit end to end (issue #476, ADR-0050): what no single slice could prove.

A strategy exits the ways a strategy can, with a reduce-only order and a cancel
all. The operator stops the engine and runs ``flatten`` for the rest. The same
scenario runs on every store and bus pair. Each life gets a new bus over the same
broker and a new store over the same backing, as a restart would.
"""

import asyncio
import json
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

import pytest
from bus_backends import BUS_BACKENDS, make_bus
from kafka_fakes import FakeKafkaBroker
from ledgers import GENESIS
from store_backends import STORE_BACKEND_PARAMS, PostgresBackend, SQLiteBackend, resolve_backend

from tickwright.adapters.clock import ManualClock
from tickwright.adapters.feed import ReplayFeed
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.app.build import paper_store_reads
from tickwright.domain import (
    EventBus,
    InstrumentSpec,
    MarketTick,
    OrderEvent,
    OrderState,
    OrderType,
    Side,
    SignalEmitter,
    Store,
    TimeInForce,
)
from tickwright.engine.exit_run import Flatten
from tickwright.engine.guard import RealGuard
from tickwright.engine.runner import Engine

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

    def engine(self, ticks: Path, *, flatten: bool = False) -> tuple[Engine, PaperExchange]:
        bus = self.bus()
        clock = ManualClock()
        store = self.backend.open()
        paper = _paper(bus, clock, store)
        engine = Engine(
            bus=bus,
            clock=clock,
            store=store,
            exchange=paper,
            feed=ReplayFeed(path=ticks, bus=bus, clock=clock),
            guard=RealGuard(specs=_SPECS, store=store, clock=clock),
            exit_job=Flatten(feed_ends=True) if flatten else None,
        )
        return engine, paper

    def venue(self) -> tuple[dict[str, Decimal], list[object]]:
        """What a restarted paper venue holds: each non-zero position, and every open order."""

        async def read(paper: PaperExchange) -> tuple[dict[str, Decimal], list[object]]:
            positions = await paper.fetch_positions()
            orders = await paper.fetch_open_orders()
            assert isinstance(positions, dict) and isinstance(orders, list)
            return {symbol: size for symbol, size in positions.items() if size != 0}, [*orders]

        with self.backend.open() as store:
            return asyncio.run(read(_paper(self.bus(), ManualClock(), store)))

    def positions(self) -> dict[tuple[str | None, str], Decimal]:
        with self.backend.open() as store:
            return {(row.strategy_id, row.symbol): row.signed_size for row in store.all_positions()}


def _paper(bus: EventBus, clock: ManualClock, store: Store) -> PaperExchange:
    reads = paper_store_reads(store)
    return PaperExchange(
        bus=bus,
        clock=clock,
        fill_model=ImmediateFillModel(),
        genesis_collateral=GENESIS,
        instrument_specs=_SPECS,
        account_net=reads.account_net,
        applied_fills=reads.applied_fills,
        resting_orders=reads.resting_orders,
    )


_STRATEGY_ROW = {"BTC": "50000", "ETH": "3000"}
_FLATTEN_ROWS = [{"BTC": "51000", "ETH": "3100"}, {"BTC": "52000", "ETH": "3200"}]


def _strategies_exit(lives: _Lives) -> None:
    """Run the strategy life until both strategies have exited, then stop it."""
    engine, _ = lives.engine(lives.ticks("strategy", [_STRATEGY_ROW] * 3, start_s=1))
    alpha = _Exiter(
        engine.emitter_for("alpha"), symbol="BTC", open_qty="0.03", reduce="0.01", rest=True
    )
    beta = _Exiter(
        engine.emitter_for("beta"), symbol="ETH", open_qty="0.5", reduce=None, rest=False
    )
    engine.register(alpha, symbols={"BTC"})
    engine.register(beta, symbols={"ETH"})

    async def go() -> int | None:
        run = asyncio.create_task(engine.run())

        async def settled() -> None:
            while not (alpha.done() and beta.done()) and not run.done():
                await asyncio.sleep(0)

        await asyncio.wait_for(settled(), timeout=5)
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
    assert lives.positions() == {
        ("alpha", "BTC"): Decimal("0.02"),
        ("beta", "ETH"): Decimal("0.5"),
    }

    engine, _ = lives.engine(lives.ticks("flatten", _FLATTEN_ROWS, start_s=10), flatten=True)

    async def go() -> int | None:
        return await asyncio.wait_for(engine.run(), timeout=5)

    assert asyncio.run(go()) == 0
    assert {key: size for key, size in lives.positions().items() if size != 0} == {}
    assert lives.venue() == ({}, [])
