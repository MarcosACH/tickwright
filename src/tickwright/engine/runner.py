"""The ``Engine`` host (ADR-0014/0024): ordered startup, supervision, shutdown.

The runner is where the lifecycle knowledge lives that would otherwise smear
across every component: the barrier-gated startup order (recover → bus →
connect the exchange → subscribe engine internals raw → reconciliation barrier
→ strategies pull-then-subscribe → feed *last*), ``asyncio.TaskGroup``
supervision, and the reverse shutdown that converges with crash recovery —
final strategy snapshots, resting ``LIVE`` orders left alone, store closed last.

The Engine consumes seam Protocols only (ADR-0032): the composition root hands
it already-built concretes and it constructs its own internals (``Cache``,
``ExecutionManager``, ``Reconciler``, ``StrategyHost``) from them. Venue-sim
wiring that is not a seam — the paper exchange filling off the tick stream — the
paper adapter owns itself (it self-subscribes at construction, ADR-0012), so
neither the Engine nor the composition root carries a paper-specific tick line;
the Engine never knows a concrete.
"""

import asyncio
import signal
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

from tickwright.domain import (
    Clock,
    ComponentState,
    EventBus,
    Exchange,
    ExecutionReport,
    MarketFeed,
    PreTradeGuard,
    Signal,
    Store,
    Strategy,
)
from tickwright.observability import NamedEvent, named_event
from tickwright.observability.correlation import bind_run_id

from .cache import Cache
from .cadence import run_cadence
from .execution import ExecutionManager
from .guard import NoopGuard
from .portfolio import PortfolioProjection
from .reconcile import ReconcileConfig, Reconciler
from .strategy_host import StrategyHost


@dataclass(frozen=True, slots=True, kw_only=True)
class EngineConfig:
    """Lifecycle timing knobs (ADR-0024 defaults)."""

    startup_reconciliation_timeout_seconds: float = 60.0
    shutdown_timeout_seconds: float = 10.0
    tick_staleness_ns: int | None = None
    reconcile: ReconcileConfig | None = None
    run_id: str | None = None
    """The correlation id for this run (ADR-0020); ``None`` generates one."""


class Engine:
    """The supervised host: ``run()`` drives the whole ADR-0024 lifecycle."""

    def __init__(
        self,
        *,
        bus: EventBus,
        clock: Clock,
        store: Store,
        exchange: Exchange,
        feed: MarketFeed,
        portfolio: PortfolioProjection,
        guard: PreTradeGuard | None = None,
        config: EngineConfig | None = None,
    ) -> None:
        self._bus = bus
        self._clock = clock
        self._store = store
        self._exchange = exchange
        self._feed = feed
        self._config = config if config is not None else EngineConfig()
        # Resolved here (not left to the ExecutionManager default) because the
        # runner also drives it from the operator signals below.
        self._guard = guard if guard is not None else NoopGuard()
        # The engine-internal components, built here from the injected seams —
        # the composition root knows the concretes, the Engine knows the wiring.
        self._cache = Cache(store=store)
        # Injected rather than built here, unlike the Cache: the composition
        # root also hands each strategy a facade scoped off this same object, so
        # it must own the one instance (ADR-0041 §7).
        self._portfolio = portfolio
        self._execution = ExecutionManager(
            bus=bus,
            clock=clock,
            exchange=exchange,
            cache=self._cache,
            portfolio=self._portfolio,
            guard=self._guard,
        )
        # Resolved here (not left to the Reconciler default) because the runner
        # also paces the continuous cadences off the same intervals below.
        self._reconcile_config = (
            self._config.reconcile if self._config.reconcile is not None else ReconcileConfig()
        )
        self._reconciler = Reconciler(
            bus=bus,
            clock=clock,
            exchange=exchange,
            cache=self._cache,
            config=self._reconcile_config,
        )
        self._host = StrategyHost(
            bus=bus, clock=clock, store=store, tick_staleness_ns=self._config.tick_staleness_ns
        )
        self._state = ComponentState.READY
        self._stop_requested = asyncio.Event()
        self._stopped = asyncio.Event()
        self._feed_task: asyncio.Task[None] | None = None
        self._cadence_tasks: list[asyncio.Task[None]] = []

    @property
    def state(self) -> ComponentState:
        return self._state

    def register(self, strategy: Strategy, *, symbols: Iterable[str]) -> None:
        """Add ``strategy`` to the hosted set before ``run()`` (ADR-0018)."""
        self._host.register(strategy, symbols=symbols)

    async def run(self) -> int:
        """The supervised lifecycle: ordered startup, operation, graceful stop.

        Returns the process exit-code contract (ADR-0024): 0 = graceful stop,
        non-zero = ``FAULTED`` — the external supervisor's restart signal.
        """
        self._install_signal_handlers()
        try:
            await self._start_sequence()
            async with asyncio.TaskGroup() as tg:
                # The continuous reconciliation cadences (ADR-0011/0024), paced
                # by virtual time (ADR-0033): they run for the life of the
                # TaskGroup and stop with the reverse shutdown below.
                self._cadence_tasks = [
                    tg.create_task(
                        run_cadence(
                            clock=self._clock,
                            interval_seconds=self._reconcile_config.inflight_interval_seconds,
                            cycle=self._reconciler.reconcile_inflight,
                        )
                    ),
                    tg.create_task(
                        run_cadence(
                            clock=self._clock,
                            interval_seconds=self._reconcile_config.open_order_interval_seconds,
                            cycle=self._reconciler.reconcile_open_orders,
                        )
                    ),
                ]
                # The feed starts last (ADR-0024 step 7): the first tick is only
                # possible after the barrier cleared, so nothing places before
                # reconciliation completes. Replay end-of-file ends the task but
                # not the run — like the CLI, the engine stops only when told to.
                named_event(NamedEvent.ENGINE_FEED_STARTED)
                self._feed_task = tg.create_task(self._feed.start())
                tg.create_task(self._stop_when_requested())
        except Exception as exc:
            # The first raw-handler exception aborted the TaskGroup and
            # cancelled its siblings (ADR-0024): fail fast, but leave a
            # readable trail and let waiters through before exiting non-zero.
            self._state = ComponentState.FAULTED
            named_event(NamedEvent.ENGINE_FAULTED, error=repr(exc))
            await self._run_best_effort_stop_hooks()
            self._stopped.set()
            return 1
        finally:
            self._remove_signal_handlers()
        self._state = ComponentState.STOPPED
        self._stopped.set()
        return 0

    async def stop(self) -> None:
        """Request the graceful stop and wait for the reverse shutdown to finish."""
        self._stop_requested.set()
        await self._stopped.wait()

    # One handler per operator verb (ADR-0024): SIGINT/SIGTERM stop the run
    # gracefully; SIGUSR1/SIGUSR2 trip and reset the durable kill switch
    # (ADR-0026) without touching the run. SIGKILL is uncatchable by design —
    # crash-only recovery on the next boot.
    _SIGNAL_VERBS = (signal.SIGINT, signal.SIGTERM, signal.SIGUSR1, signal.SIGUSR2)

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, self._stop_requested.set)
        loop.add_signal_handler(signal.SIGTERM, self._stop_requested.set)
        loop.add_signal_handler(signal.SIGUSR1, self._trip_kill_switch)
        loop.add_signal_handler(signal.SIGUSR2, self._guard.reset_kill_switch)

    def _remove_signal_handlers(self) -> None:
        """Restore default dispositions so a stopped engine never holds the
        process's signal surface (and a test process gets its own back)."""
        loop = asyncio.get_running_loop()
        for sig in self._SIGNAL_VERBS:
            loop.remove_signal_handler(sig)

    def _trip_kill_switch(self) -> None:
        self._guard.trip_kill_switch("SIGUSR1: operator kill switch")

    async def _start_sequence(self) -> None:
        """ADR-0024 steps 1–6: everything before the feed, in order."""
        # Bind the per-process run correlation first: every record from here on
        # — the engine's and every component's — is traceable to this run.
        run_id = self._config.run_id or f"run-{uuid.uuid4().hex[:12]}"
        bind_run_id(run_id)
        # Recover: rebuild the read-model projection from the durable record.
        self._cache.rebuild()
        # Start the bus (ADR-0024 step 3): in-memory a no-op; Kafka connects
        # the producer/consumer — before anything can publish or subscribe.
        await self._bus.start()
        # Connect the exchange (ADR-0024 step 4), after the bus so an adapter
        # that reports on it has somewhere to publish, and before the barrier
        # below so a refusal precedes any order and the barrier's own venue
        # reads observe an already-aligned venue.
        await self._exchange.start()
        # Engine-internal handlers subscribe raw (ADR-0024): any exception in
        # the saga path propagates to the TaskGroup and faults the engine.
        self._bus.subscribe(Signal, self._execution.on_signal)
        self._bus.subscribe(ExecutionReport, self._execution.on_execution_report)
        # The hard gate: nothing places until reconciliation succeeds.
        await self._reconciler.run_startup_barrier(
            timeout_seconds=self._config.startup_reconciliation_timeout_seconds
        )
        named_event(NamedEvent.ENGINE_BARRIER_CLEARED)
        # Strategies after the barrier: restore snapshot, resume seq, subscribe.
        self._host.start()
        self._state = ComponentState.RUNNING

    def _teardown_steps(self) -> tuple[tuple[str, Callable[[], Awaitable[None] | None]], ...]:
        """The reverse shutdown, described once (ADR-0024).

        Both teardown paths walk this same ordered membership and differ only
        in failure *policy* (graceful: bounded as a whole, propagate; faulted:
        bounded per step, record, keep going) — so a new teardown seam adds
        one entry here, never a second copy that the fault path silently
        misses. Order: cut the source, silence the cycles that read the venue,
        release the venue, let in-flight events land in the final snapshots
        (ADR-0016), then flush the bus and close the store last — resting LIVE
        orders stay in it for restart reconciliation to re-adopt (crash and
        graceful stop converge on one recovery path).

        The venue is released *behind* the cadences deliberately: they read it
        (``fetch_order``), so releasing it first would leave a live cycle
        querying an adapter this very sequence had just torn down. It stays
        ahead of the drain, because a loop the adapter owns would otherwise
        publish into that drain and keep raising its high-water mark, so the
        cascade never quiesces — both ends of the slot are load-bearing. The
        drain still dispatches behind the release, and ``host.stop`` only
        snapshots, so a late ``Signal`` can still reach the venue: the seam
        answers for that (``Exchange.stop``), not the order.

        One membership walked twice is the cost of one membership: the faulted
        pass restarts at the top, so a graceful step that raises drives every
        step *ahead* of the break a second time. Every entry here is therefore
        specified idempotent at its own seam — none may treat a second call as
        an error, in the one window where nothing can act on it.
        """
        return (
            ("feed.stop", self._stop_feed),
            ("reconcile.stop", self._stop_cadences),
            ("exchange.stop", self._exchange.stop),
            ("bus.drain", self._bus.drain),
            ("host.stop", self._host.stop),
            ("bus.close", self._bus.close),
            ("store.close", self._store.close),
        )

    async def _stop_cadences(self) -> None:
        """Cancel the continuous reconciliation loops and wait them out, so no
        cycle is still publishing heals while the bus drains and the store
        closes behind it. On the fault path the TaskGroup already cancelled
        them; cancelling a done task is a no-op."""
        for task in self._cadence_tasks:
            task.cancel()
        await asyncio.gather(*self._cadence_tasks, return_exceptions=True)

    async def _stop_feed(self) -> None:
        """Ask the feed to stop, then cancel a read loop that outlives the ask
        (a live feed mid-read never returns on its own). On the fault path the
        TaskGroup already cancelled the task; the venue connection still needs
        the explicit ``stop`` so a live WS cannot leak."""
        await self._feed.stop()
        if self._feed_task is not None and not self._feed_task.done():
            self._feed_task.cancel()

    @staticmethod
    async def _run_step(step: Callable[[], Awaitable[None] | None]) -> None:
        result = step()
        if result is not None:
            await result

    async def _stop_when_requested(self) -> None:
        """Wait for the stop request, then reverse the startup (ADR-0024).

        Bounded by ``shutdown_timeout``: a teardown that cannot finish (a
        wedged venue connection, say) raises out to the TaskGroup and faults
        non-zero rather than wedging the process. The bound is wall-clock
        deliberately — it guards against real hangs, not simulated time.
        """
        await self._stop_requested.wait()
        async with asyncio.timeout(self._config.shutdown_timeout_seconds):
            for _name, step in self._teardown_steps():
                await self._run_step(step)

    async def _run_best_effort_stop_hooks(self) -> None:
        """The faulted teardown: the same steps, best-effort per step.

        A fault must still stop the feed, take the last strategy snapshots,
        flush the bus, and close the store where it can — but a failing or
        hanging step cannot be allowed to mask the fault or block the non-zero
        exit, so each step is bounded on its own and a break is *recorded*
        (never swallowed silently): a lost snapshot or an unclosed store on
        the fault path is exactly the kind of thing an operator must be able
        to see in the trail (ADR-0020), riding the same run correlation.
        """
        for name, step in self._teardown_steps():
            try:
                async with asyncio.timeout(self._config.shutdown_timeout_seconds):
                    await self._run_step(step)
            except Exception as exc:
                named_event(NamedEvent.ENGINE_STOP_HOOK_FAILED, hook=name, error=repr(exc))
