"""``StrategyHost`` — the engine-side strategy runtime (issue #17).

Hosts N third-party ``Strategy`` instances behind the engine's safety net
(ADR-0018): a registry, and per-strategy subscription wrappers that route each
strategy only the events it declared an interest in. Routing/filtering is a
wrapper concern, never a bus feature — pub/sub stays type-keyed (ADR-0024).
"""

from collections.abc import Iterable

from tickwright.domain import (
    Clock,
    EventBus,
    InvariantViolation,
    MarketTick,
    OrderEvent,
    Strategy,
)


class StrategyHost:
    """Registers strategies and wires their wrapped bus subscriptions."""

    def __init__(
        self, *, bus: EventBus, clock: Clock, tick_staleness_ns: int | None = None
    ) -> None:
        self._bus = bus
        self._clock = clock
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
        """Subscribe every registered strategy's wrapped handlers."""
        for strategy in self._strategies.values():
            self._subscribe(strategy)

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
            await strategy.on_tick(tick)

        async def on_order_event(event: OrderEvent) -> None:
            # Events return only to the owning strategy (ADR-0018): another
            # strategy's saga transitions are never its business.
            if event.strategy_id != strategy.strategy_id:
                return
            await strategy.on_order_event(event)

        self._bus.subscribe(MarketTick, on_tick)
        self._bus.subscribe(OrderEvent, on_order_event)
