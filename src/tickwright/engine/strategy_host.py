"""``StrategyHost`` — the engine-side strategy runtime (issue #17).

Hosts N third-party ``Strategy`` instances behind the engine's safety net
(ADR-0018): a registry, and per-strategy subscription wrappers that route each
strategy only the events it declared an interest in. Routing/filtering is a
wrapper concern, never a bus feature — pub/sub stays type-keyed (ADR-0024).
"""

from collections.abc import Iterable

from tickwright.domain import Clock, EventBus, MarketTick, Strategy


class StrategyHost:
    """Registers strategies and wires their wrapped bus subscriptions."""

    def __init__(self, *, bus: EventBus, clock: Clock) -> None:
        self._bus = bus
        self._clock = clock
        self._strategies: dict[str, Strategy] = {}
        self._symbols: dict[str, frozenset[str]] = {}

    def register(self, strategy: Strategy, *, symbols: Iterable[str]) -> None:
        """Add ``strategy`` to the registry with its declared symbol set."""
        self._strategies[strategy.strategy_id] = strategy
        self._symbols[strategy.strategy_id] = frozenset(symbols)

    def start(self) -> None:
        """Subscribe every registered strategy's wrapped handlers."""
        for strategy in self._strategies.values():
            self._subscribe(strategy)

    def _subscribe(self, strategy: Strategy) -> None:
        symbols = self._symbols[strategy.strategy_id]

        async def on_tick(tick: MarketTick) -> None:
            if tick.symbol not in symbols:
                return
            await strategy.on_tick(tick)

        self._bus.subscribe(MarketTick, on_tick)
