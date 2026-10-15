"""Paper venues and a clock for suites that drive an exit run across engine lives.

A restart test needs paper that rebuilds from the store, as the composition root
builds it. A crash test needs one venue that outlives every life. A live flatten
test needs a clock that holds the run in its wait for a first price. Each lived
in more than one suite before, and the copies had started to drift.

Paper is a venue, so doubling it is legitimate under ADR-0022.
"""

import asyncio
from collections.abc import Mapping

from ledgers import GENESIS

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.app.build import paper_store_reads
from tickwright.domain import (
    Clock,
    EventBus,
    ExecutionReport,
    InstrumentSpec,
    MarketTick,
    Store,
)
from tickwright.engine.exit_run import DRY_WAIT_SECONDS


def paper_on(
    store: Store, *, bus: EventBus, clock: Clock, specs: Mapping[str, InstrumentSpec]
) -> PaperExchange:
    """Paper that rebuilds its book and fills from ``store``, as after a restart."""
    reads = paper_store_reads(store)
    return PaperExchange(
        bus=bus,
        clock=clock,
        fill_model=ImmediateFillModel(),
        genesis_collateral=GENESIS,
        instrument_specs=specs,
        account_net=reads.account_net,
        applied_fills=reads.applied_fills,
        resting_orders=reads.resting_orders,
    )


class LastingVenue:
    """Paper on a bus of its own, so it outlives the engine lives it serves.

    A new paper venue rebuilds only from order rows the store holds (#484). A
    crashed life can leave an order at the venue that the store never booked,
    so a crash test keeps one venue across every life.
    """

    def __init__(self, *, specs: Mapping[str, InstrumentSpec], store: Store | None = None) -> None:
        self.bus = InMemoryBus()
        self._store = store
        self._engine_bus: EventBus | None = None
        self.paper = PaperExchange(
            bus=self.bus,
            clock=ManualClock(),
            fill_model=ImmediateFillModel(),
            genesis_collateral=GENESIS,
            instrument_specs=specs,
            # Read through the property, so paper always reads the store of the life it serves.
            account_net=lambda: paper_store_reads(self.store).account_net(),
            applied_fills=lambda cloid: paper_store_reads(self.store).applied_fills(cloid),
            resting_orders=lambda: paper_store_reads(self.store).resting_orders(),
        )
        self.bus.subscribe(ExecutionReport, self._report)

    @property
    def store(self) -> Store:
        """The store paper reads. A restart test reopens it between lives."""
        assert self._store is not None, "no store yet: serve a life first"
        return self._store

    @store.setter
    def store(self, store: Store) -> None:
        self._store = store

    def serve(self, engine_bus: EventBus | None, store: Store | None = None) -> PaperExchange:
        """Link the venue to a new life: its ticks in, the venue's reports out.

        ``store`` replaces the one paper reads. ``None`` for ``engine_bus`` links
        no engine, for a test that only reads the venue.
        """
        if store is not None:
            self._store = store
        self._engine_bus = engine_bus
        if engine_bus is not None:

            async def tick(tick: MarketTick) -> None:
                await self.bus.publish(tick)

            engine_bus.subscribe(MarketTick, tick)
        return self.paper

    def cut(self) -> None:
        """The engine dies. Nothing the venue says from here reaches it."""
        self._engine_bus = None

    async def _report(self, report: ExecutionReport) -> None:
        if self._engine_bus is not None:
            await self._engine_bus.publish(report)


class PriceWaitBlocks(ManualClock):
    """A ``ManualClock`` whose dry wait never ends. ``waiting`` is set once one starts.

    A live flatten's first dry wait is its wait for a symbol's first price. So a
    flatten on this clock holds its store until it is stopped.
    """

    def __init__(self) -> None:
        super().__init__()
        self.waiting = asyncio.Event()

    async def sleep(self, seconds: float) -> None:
        if seconds == DRY_WAIT_SECONDS:
            self.waiting.set()
            await asyncio.Event().wait()
        await super().sleep(seconds)
