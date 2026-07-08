"""The ``Engine`` host (ADR-0014/0024): ordered startup, supervision, shutdown.

The runner is where the lifecycle knowledge lives that would otherwise smear
across every component: the barrier-gated startup order (recover → subscribe
engine internals raw → reconciliation barrier → strategies pull-then-subscribe
→ feed *last*), ``asyncio.TaskGroup`` supervision, and the reverse shutdown
that converges with crash recovery — final strategy snapshots, resting ``LIVE``
orders left alone, store closed last.

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
from collections.abc import Iterable
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
from .execution import ExecutionManager
from .guard import NoopGuard
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
        self._execution = ExecutionManager(
            bus=bus, clock=clock, exchange=exchange, cache=self._cache, guard=self._guard
        )
        self._reconciler = Reconciler(
            bus=bus,
            clock=clock,
            exchange=exchange,
            cache=self._cache,
            config=self._config.reconcile,
        )
        self._host = StrategyHost(
            bus=bus, clock=clock, store=store, tick_staleness_ns=self._config.tick_staleness_ns
        )
        self._state = ComponentState.READY
        self._stop_requested = asyncio.Event()
        self._stopped = asyncio.Event()

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
                # The feed starts last (ADR-0024 step 7): the first tick is only
                # possible after the barrier cleared, so nothing places before
                # reconciliation completes. Replay end-of-file ends the task but
                # not the run — like the CLI, the engine stops only when told to.
                named_event(NamedEvent.ENGINE_FEED_STARTED)
                feed_task = tg.create_task(self._feed.start())
                tg.create_task(self._stop_when_requested(feed_task))
        except Exception as exc:
            # The first raw-handler exception aborted the TaskGroup and
            # cancelled its siblings (ADR-0024): fail fast, but leave a
            # readable trail and let waiters through before exiting non-zero.
            self._state = ComponentState.FAULTED
            named_event(NamedEvent.ENGINE_FAULTED, error=repr(exc))
            self._run_best_effort_stop_hooks()
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

    async def _stop_when_requested(self, feed_task: asyncio.Task[None]) -> None:
        """Wait for the stop request, then reverse the startup (ADR-0024).

        Bounded by ``shutdown_timeout``: a teardown that cannot finish (a
        wedged venue connection, say) raises out to the TaskGroup and faults
        non-zero rather than wedging the process. The bound is wall-clock
        deliberately — it guards against real hangs, not simulated time.
        """
        await self._stop_requested.wait()
        async with asyncio.timeout(self._config.shutdown_timeout_seconds):
            await self._feed.stop()
            if not feed_task.done():
                feed_task.cancel()
            # Final snapshots (ADR-0016), then the store closes last: every
            # checkpoint is already durable, and resting LIVE orders stay in it
            # — restart reconciliation re-adopts them (crash and graceful stop
            # converge on one recovery path).
            self._host.stop()
            self._store.close()

    def _run_best_effort_stop_hooks(self) -> None:
        """The faulted teardown: try each stop hook, keep going if one breaks.

        A fault must still leave the last strategy snapshots and a closed store
        where it can — but a failing hook cannot be allowed to mask the fault
        or block the non-zero exit. A hook that breaks is *recorded* (never
        swallowed silently): a lost snapshot or an unclosed store on the fault
        path is exactly the kind of thing an operator must be able to see in the
        trail (ADR-0020), and it rides the same run correlation as the fault.
        """
        for hook in (self._host.stop, self._store.close):
            try:
                hook()
            except Exception as exc:
                named_event(NamedEvent.ENGINE_STOP_HOOK_FAILED, hook=hook.__name__, error=repr(exc))
                continue
