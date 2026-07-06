"""``StrategyHost`` — the engine-side strategy runtime (issue #17).

Hosts N third-party ``Strategy`` instances behind the engine's safety net
(ADR-0018): a registry, and per-strategy subscription wrappers that route each
strategy only the events it declared an interest in. Routing/filtering is a
wrapper concern, never a bus feature — pub/sub stays type-keyed (ADR-0024).
"""

from collections.abc import Awaitable, Callable, Iterable

from tickwright.domain import (
    Clock,
    Event,
    EventBus,
    InvariantViolation,
    MarketTick,
    OrderEvent,
    Store,
    Strategy,
)
from tickwright.observability import named_event


class StrategyHost:
    """Registers strategies and wires their wrapped bus subscriptions."""

    def __init__(
        self,
        *,
        bus: EventBus,
        clock: Clock,
        store: Store,
        tick_staleness_ns: int | None = None,
    ) -> None:
        self._bus = bus
        self._clock = clock
        self._store = store
        # The staleness gate (ADR-0025): a tick older than this against the
        # clock is a redelivered backlog — dropped, so a restart cannot trade
        # on pre-crash prices. ``None`` disables it; the live composition root
        # picks the policy, the paper/replay path needs none (the ReplayFeed
        # advances the clock to each tick, so replay ticks are never stale).
        self._tick_staleness_ns = tick_staleness_ns
        self._strategies: dict[str, Strategy] = {}
        self._symbols: dict[str, frozenset[str]] = {}

    def register(self, strategy: Strategy, *, symbols: Iterable[str]) -> None:
        """Add ``strategy`` to the registry with its declared symbol set.

        A duplicate ``strategy_id`` is a composition-root wiring bug: ids key
        seqs, snapshots, and ``OrderEvent`` routing, so two strategies sharing
        one would silently corrupt each other's state (ADR-0018 — fail fast).
        """
        if strategy.strategy_id in self._strategies:
            raise InvariantViolation(f"duplicate strategy_id registered: {strategy.strategy_id}")
        self._strategies[strategy.strategy_id] = strategy
        self._symbols[strategy.strategy_id] = frozenset(symbols)

    def start(self) -> None:
        """Restore each strategy's persisted state, then subscribe its wrapped
        handlers — restore strictly before the first delivered event."""
        for strategy in self._strategies.values():
            self._restore(strategy)
            self._subscribe(strategy)

    def _restore(self, strategy: Strategy) -> None:
        """Feed the persisted snapshot to ``strategy.restore()``, if one exists.

        A restore failure is *not* an invariant violation (ADR-0016): the
        strategy's code changed shape between runs — log
        ``strategy.snapshot_incompatible`` and start fresh. Seq-safety is
        unaffected; it comes from the saga store, never the snapshot.
        """
        data = self._store.load_strategy_snapshot(strategy.strategy_id)
        if data is None:
            return
        try:
            strategy.restore(data)
        except InvariantViolation:
            raise
        except Exception as exc:
            named_event(
                "strategy.snapshot_incompatible",
                strategy_id=strategy.strategy_id,
                error=repr(exc),
            )

    def stop(self) -> None:
        """Take the final ``snapshot()`` of every strategy and persist it.

        The graceful-stop half of ADR-0016's cadence: the engine owns
        durability, so the strategy's last state content outlives the process.
        """
        for strategy in self._strategies.values():
            self._store.save_strategy_snapshot(
                strategy.strategy_id, strategy.snapshot(), ts_ns=self._clock.timestamp_ns()
            )

    def _subscribe(self, strategy: Strategy) -> None:
        symbols = self._symbols[strategy.strategy_id]
        # The per-symbol monotonic gate (ADR-0025): the last (ts_event, trade_id)
        # dispatched to this strategy, per symbol. A tick at or below it is a
        # redelivery or a reorder — dropped, so on_tick is structurally
        # idempotent with no strategy-author effort. Sound because per-symbol
        # ordering (ADR-0003) means a duplicate can only arrive in order.
        high_water: dict[str, tuple[int, str]] = {}

        async def on_tick(tick: MarketTick) -> None:
            if tick.symbol not in symbols:
                return
            mark = (tick.ts_event, tick.trade_id)
            last = high_water.get(tick.symbol)
            if last is not None and mark <= last:
                return
            if (
                self._tick_staleness_ns is not None
                and self._clock.timestamp_ns() - tick.ts_event > self._tick_staleness_ns
            ):
                return
            high_water[tick.symbol] = mark
            await self._contained(strategy, strategy.on_tick, tick)

        async def on_order_event(event: OrderEvent) -> None:
            # Events return only to the owning strategy (ADR-0018): another
            # strategy's saga transitions are never its business.
            if event.strategy_id != strategy.strategy_id:
                return
            await self._contained(strategy, strategy.on_order_event, event)

        self._bus.subscribe(MarketTick, on_tick)
        self._bus.subscribe(OrderEvent, on_order_event)

    @staticmethod
    async def _contained[E: Event](
        strategy: Strategy, handler: Callable[[E], Awaitable[None]], event: E
    ) -> None:
        """Run a third-party handler inside the containment net (ADR-0024).

        A strategy bug is logged as ``strategy.error`` — correlated by the
        triggering event's identity — and the engine continues; one bad handler
        must never fault the engine or starve its sibling strategies. Only an
        ``InvariantViolation`` (a broken *engine* assumption) pierces and
        faults, and ``BaseException`` (cancellation, exit) is never swallowed.
        """
        try:
            await handler(event)
        except InvariantViolation:
            raise
        except Exception as exc:
            named_event(
                "strategy.error",
                strategy_id=strategy.strategy_id,
                event_id=event.event_id,
                error=repr(exc),
            )
