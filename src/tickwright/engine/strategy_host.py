"""``StrategyHost`` — the engine-side strategy runtime (issue #17).

Hosts N third-party ``Strategy`` instances behind the engine's safety net:
a unique-id registry (ADR-0018), per-strategy subscription wrappers that
route each strategy only its declared symbols' ticks and its own
``OrderEvent``s, the per-symbol monotonic tick gate plus staleness threshold
(ADR-0025), the origin-based containment net (ADR-0024), and the
snapshot-persist/restore and seq high-water recovery of ADR-0016. Routing,
gating, and containment are wrapper concerns, never bus features — pub/sub
stays type-keyed. The engine runner (issue #19) composes this host into the
full ADR-0024 lifecycle.
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
    signal_seq,
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
        """Recover each strategy — snapshot, then seq, then subscriptions.

        The seq high-water mark comes from the saga store, never the snapshot
        (ADR-0016): a stale snapshot restored a moment earlier can therefore
        never re-emit a consumed ``signal_id``.
        """
        high_water = self._seq_high_water()
        for strategy in self._strategies.values():
            self._restore(strategy)
            strategy.set_next_seq(high_water.get(strategy.strategy_id, 0) + 1)
            self._subscribe(strategy)

    def _seq_high_water(self) -> dict[str, int]:
        """Max consumed seq per strategy across every checkpointed saga.

        Every place consumed its ``signal_id``'s seq, and every requested
        cancel consumed its own (ADR-0026 — omitting cancels would let a
        restart reuse a consumed id). One per-strategy counter across all
        symbols, so this is a single max over the strategy's records.
        """
        high_water: dict[str, int] = {}
        for order in self._store.all_orders():
            seqs = [signal_seq(order.signal_id)]
            if order.cancel_signal_id is not None:
                seqs.append(signal_seq(order.cancel_signal_id))
            current = high_water.get(order.strategy_id, 0)
            high_water[order.strategy_id] = max(current, *seqs)
        return high_water

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
