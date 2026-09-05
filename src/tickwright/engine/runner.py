"""The ``Engine`` host (ADR-0014/0024): ordered startup, supervision, shutdown.

The runner is where the lifecycle knowledge lives that would otherwise smear
across every component: the barrier-gated startup order (recover the ledger →
rebuild the order cache → bus → connect the exchange → subscribe engine
internals raw → reconciliation barrier → strategies pull-then-subscribe → feed
*last*), ``asyncio.TaskGroup``
supervision, and the reverse shutdown that converges with crash recovery —
final strategy snapshots, resting ``LIVE`` orders left alone, store closed last.

The Engine consumes seam Protocols only (ADR-0032): the composition root hands
it already-built concretes and it constructs its own internals (``Checkpointer``,
``ExecutionManager``, ``Reconciler``, ``StrategyHost``) from them — the two
read-models included, arriving as the one ``Checkpointer`` that holds them, so
the order cache and the ledger cannot be pointed at different stores and the
fill's write is one transaction by construction (ADR-0043 §4). Venue-sim
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
    EMPTY_LEVERAGE_BOOK,
    Clock,
    ComponentState,
    EventBus,
    Exchange,
    ExecutionReport,
    FundingAccrual,
    LeverageBook,
    MarketFeed,
    MarkTick,
    Portfolio,
    PreTradeGuard,
    Signal,
    Store,
    Strategy,
)
from tickwright.observability import NamedEvent, named_event
from tickwright.observability.correlation import bind_run_id

from .barrier import StartupBarrier
from .cadence import run_cadence
from .checkpoint import Checkpointer
from .execution import ExecutionManager
from .guard import NoopGuard
from .ledger_reconcile import LedgerReconciliation, ValuationBand
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
    band: ValuationBand = ValuationBand()
    """The Tier-2 alert tolerance (ADR-0040 §6), the one knob its cycle owns.

    Whole, not ``| None`` like ``reconcile`` beside it: that one is optional
    because the runner *also* paces its own cadences off it and so has to
    resolve it here either way, while this one is read once and handed
    straight on — an unconfigured run and a run configured to the defaults are
    the same run, so there is nothing for the ``None`` to mean.
    """

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
        leverage: LeverageBook = EMPTY_LEVERAGE_BOOK,
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
        # Both read-models come from one object built on the one ``store`` above,
        # so an Engine whose order cache and ledger write different stores is not
        # constructible and the fill's write is one transaction by construction
        # (ADR-0043 §4). Which account the ledger opens against is the venue's
        # own declaration, taken off the ``Exchange`` seam — and so is the
        # instrument universe the margin model values against, the exact peer
        # accessor beside it (ADR-0040 §4). Both are read here rather than
        # injected by the composition root, because the runner already holds the
        # ``Exchange`` and a second path for either would be a second thing that
        # could point the ledger at a venue the orders do not go to. The guard
        # takes its copy of the same universe from the root (ADR-0031), which is
        # the one placement where both sides are concrete.
        self._checkpointer = Checkpointer(
            spec=exchange.account_spec(),
            store=store,
            clock=clock,
            leverage=leverage,
            specs=exchange.instrument_specs(),
        )
        self._execution = ExecutionManager(
            bus=bus, exchange=exchange, checkpointer=self._checkpointer, guard=self._guard
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
            cache=self._checkpointer.cache,
            config=self._reconcile_config,
        )
        # The account grain's own cycle (ADR-0034), constructed on every path
        # and *scheduled* on one: it is the live/paper split that is conditional,
        # not the object, so the predicate lives at the single place the split
        # is real — the cadence below — rather than turning this attribute
        # ``None`` and handing every later reader a second thing to unwrap.
        self._ledger_reconciler = LedgerReconciliation(
            exchange=exchange, checkpointer=self._checkpointer, band=self._config.band
        )
        self._host = StrategyHost(
            bus=bus, clock=clock, store=store, tick_staleness_ns=self._config.tick_staleness_ns
        )
        self._state = ComponentState.READY
        self._stop_requested = asyncio.Event()
        self._stopped = asyncio.Event()
        self._feed_task: asyncio.Task[None] | None = None
        self._exchange_task: asyncio.Task[None] | None = None
        self._cadence_tasks: list[asyncio.Task[None]] = []

    @property
    def state(self) -> ComponentState:
        return self._state

    @property
    def portfolio(self) -> PortfolioProjection:
        """The accounting read-model, lent for reads by everything that is not a
        strategy — telemetry, the CLI, and the operator surfaces ADR-0041 §8
        points at the ``engine`` concrete rather than at the scoped seam.

        A strategy gets ``portfolio_for`` below instead: the ``Portfolio``
        Protocol takes no scope argument precisely because the facade is already
        bound to one, and handing a strategy this would hand it the reserved
        unattributed partition the facade exists to keep unreachable.
        """
        return self._checkpointer.portfolio

    def portfolio_for(self, strategy_id: str) -> Portfolio:
        """The scoped read-facade to hand ``strategy_id``'s constructor.

        The composition root still does the injecting (ADR-0041 §7) — a strategy
        receives its facade as an ``__init__`` argument, not from the engine and
        not through the ``Strategy`` Protocol — but it resolves the facade here,
        off the one projection the engine writes, rather than off a projection of
        its own. Only the facade leaves: the concrete stays engine-internal, as
        the ``Cache`` does.
        """
        return self._checkpointer.portfolio.for_strategy(strategy_id)

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
                # The account grain joins them on the **live path alone**
                # (ADR-0034): paper has no second account to compare the ledger
                # against, and `PaperExchange` answers this read `None` by
                # construction — the fail-closed value — so a cadence scheduled
                # there would freeze every cycle and report the default path as
                # an outage. The predicate is the venue's own declaration, the
                # same `declares_genesis` the startup checks read (ADR-0042 §6):
                # declared on paper, ingested on live. It is the
                # venue kind and not the row, which is what separates it from
                # the barrier's step one grain up — that one asks whether a
                # *read is owed* and a live restart answers no, while this asks
                # whether there is anything to reconcile against at all, and the
                # answer holds for every cycle of the run.
                if not self._exchange.account_spec().declares_genesis:
                    self._cadence_tasks.append(
                        tg.create_task(
                            run_cadence(
                                clock=self._clock,
                                interval_seconds=self._reconcile_config.account_interval_seconds,
                                cycle=self._ledger_reconciler.reconcile_account,
                            )
                        )
                    )
                # The venue's own long-lived half (ADR-0037's paper funding
                # generator, today), supervised beside the cadences rather than
                # spawned by the adapter: an exception raised in it aborts this
                # group and faults the run at the refusal, which is the whole
                # of why the seam declares ``run`` separately from ``start``.
                # Awaiting it at step 4 instead is not available — that step
                # must return so the barrier can run — and a task the adapter
                # created for itself would have no fault channel at all.
                # An adapter with no loop returns here immediately and its task
                # simply completes; nothing downstream distinguishes the two.
                self._exchange_task = tg.create_task(self._exchange.run())
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
        # Restore both read-models from the durable record, ledger first — the
        # ordering is ADR-0043 §6/§10's and belongs to the ``Checkpointer``,
        # which is why the rule is stated there rather than reproduced here.
        # What this step still owns is *when* it runs: ahead of everything else,
        # so paper's genesis row is written long before ``host.start()`` lets a
        # strategy read a cash line.
        self._checkpointer.recover()
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
        # Funding is the one accounting input that arrives on the bus rather
        # than on the fill-apply path, because it has no carrier fill (ADR-0037)
        # — so it is subscribed here, beside the saga's own raw handlers, and it
        # inherits their containment in full. That containment is not a property
        # of subscribing raw: it holds only because the *publisher* is a
        # supervised task, which for this event is ``Exchange.run()``, created
        # in the TaskGroup below. A refused ledger write therefore faults the
        # engine at the refusal and exits non-zero, rather than killing a
        # generator the adapter had spawned for itself and leaving the run
        # accruing nothing behind a still-``RUNNING`` state (#226).
        #
        # Subscribed **before** the exchange can produce one, and now trivially
        # so: the generator does not exist until the TaskGroup opens, which is
        # behind this line and behind the barrier.
        self._bus.subscribe(FundingAccrual, self._on_funding_accrual)
        # The mark is the *other* input the ledger subscribes to, and it is the
        # gentler of the two: it moves no money and reaches no store, so unlike
        # the accrual above there is nothing here a refusal could leave half
        # done. Subscribed beside it because the wiring is the runner's either
        # way — which event reaches which verb — and subscribed **before** the
        # feed starts at step 7, so no mark can be published to nobody.
        self._bus.subscribe(MarkTick, self._on_mark_tick)
        # The hard gate: nothing places until every proof it holds has cleared.
        # The sequence is the ordering rule, and it lives here rather than inside
        # any step — lifecycle ordering is the runner's, and the barrier owns
        # only the retry-then-fault policy the steps share. Both steps are bound
        # methods on the component owning the grain each one proves, so what the
        # runner contributes is the tuple's order and nothing else.
        #
        # The account row is materialised **before** the mass-rebuild rather than
        # merely inside the same barrier (ADR-0043 §6): the rebuild emits
        # synthetic fills, every fill's write carries the account row (§9 — every
        # mutation moves cash), so a rebuild that ran first would *create* the
        # live row itself at the zero ``Account.open`` resolves a ``None``
        # genesis to. The materialisation behind it would then decline to
        # overwrite a row that exists, and that zero would stand for the life of
        # the ledger with nothing to refuse it — #188's genesis comparison is
        # paper-only. The live row must be materialised, never fallen into.
        await StartupBarrier(
            clock=self._clock,
            steps=(
                self._ledger_reconciler.materialise_account,
                self._reconciler.reconcile_startup,
            ),
        ).run(timeout_seconds=self._config.startup_reconciliation_timeout_seconds)
        named_event(NamedEvent.ENGINE_BARRIER_CLEARED)
        # Strategies after the barrier: restore snapshot, resume seq, subscribe.
        self._host.start()
        self._state = ComponentState.RUNNING

    async def _on_funding_accrual(self, accrual: FundingAccrual) -> None:
        """Apply one settled boundary to the ledger — the bus's only ledger entry.

        The adapting layer between an ``async`` subscriber and a synchronous
        write verb, and it is deliberately nothing more. The gate, the split and
        the single transaction all belong to the ``Checkpointer``, which owns
        every ordering a caller could invert; what this owns is the *wiring* —
        which event reaches which verb — which is the runner's, exactly as the
        two saga subscriptions above are.

        Synchronous inside, with no ``await`` between the fold and the write, so
        no other handler can observe a ledger whose memory and durable states
        disagree — the same no-yield discipline the fill path keeps.
        """
        self._checkpointer.checkpoint_funding(accrual)

    async def _on_mark_tick(self, mark: MarkTick) -> None:
        """Hand one mark to the ledger's latest-value cache (ADR-0039).

        The adapting layer between an ``async`` subscriber and a synchronous
        verb, as ``_on_funding_accrual`` is — and nothing more. It goes to the
        projection directly rather than through the ``Checkpointer``, and the
        line is the same one that type draws for itself: what it owns is an
        ordering a caller could invert, and taking a mark is one in-memory
        write with no store behind it and no ordering inside it.

        The provenance-agnosticism is the feed's doing, not this method's: paper
        and live both publish a ``MarkTick``, so there is no branch here and
        nothing to tell the two deployments apart.
        """
        self._checkpointer.portfolio.observe_mark(mark)

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
        ahead of the drain, because the venue's supervised long-lived half would
        otherwise publish into that drain and keep raising its high-water mark,
        so the cascade never quiesces — both ends of the slot are load-bearing.
        That half is the runner's to end rather than the adapter's, so the entry
        is ``_stop_exchange``: the seam's ``stop`` plus the cancellation, exactly
        as ``feed.stop`` is one entry over two things. The
        drain still dispatches behind the release, and ``host.stop`` only
        snapshots, so a late ``Signal`` can still reach the venue: the seam
        answers for that (``Exchange.stop``), not the order.

        One membership walked twice is the cost of one membership: the faulted
        pass restarts at the top, so a graceful step that raises drives every
        step *ahead* of the break a second time. No entry here may treat a
        second call as an error, in the one window where nothing can act on it.
        The five that cross a seam say so at that seam — ``MarketFeed.stop``,
        ``Exchange.stop``, ``EventBus.drain``, ``EventBus.close``,
        ``Store.close``; the other two are the engine's own and answer for it
        here (``_stop_cadences`` re-cancels tasks already done, a no-op, and
        ``StrategyHost.stop`` retakes the final snapshots into the same
        latest-wins row per strategy — a rewrite, not a second effect). The two
        entries that are both — ``feed.stop`` and ``exchange.stop`` wrap a seam
        call *and* a task cancellation — inherit the property from each half.
        """
        return (
            ("feed.stop", self._stop_feed),
            ("reconcile.stop", self._stop_cadences),
            ("exchange.stop", self._stop_exchange),
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

    async def _stop_exchange(self) -> None:
        """Release the venue, then end the long-lived half this runner supervises
        — the ``_stop_feed`` shape one seam over, and one membership entry for
        the same reason: a seam whose loop the runner owns is still *one* seam.

        Cancelled and **waited out**, which the feed's task is not — and the
        difference is *how each loop ends*, not what it publishes (both do).
        ``MarketFeed.stop`` is a cooperative signal a live feed's loop reads and
        returns on, so the cancel behind it is belt-and-braces; the generator has
        no such signal, so cancellation is the only thing that ends it and the
        wait is the only thing that proves it did. That proof is what this slot
        owes the bus drain below it: anything still publishing keeps raising the
        drain's high-water mark, so cancelling without waiting would leave the
        guarantee to whichever turn the loop happened to be on. Exceptions are
        absorbed here rather than raised: a task that died of its own accord
        already reached the ``TaskGroup``, which is what faulted the run and is
        where that failure belongs — reporting it a second time out of the
        teardown would name a hook for a refusal that happened long before it.
        On the fault path the group already cancelled it, and cancelling a done
        task is a no-op.
        """
        await self._exchange.stop()
        if self._exchange_task is not None:
            self._exchange_task.cancel()
            await asyncio.gather(self._exchange_task, return_exceptions=True)

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
