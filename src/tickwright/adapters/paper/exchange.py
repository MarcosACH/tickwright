"""``PaperExchange`` — the deterministic in-process ``Exchange`` and default v1
target (ADR-0012).

A *real* exchange, never a mock: it caches the latest tick per symbol, fills a
MARKET order on receipt against that price, and holds a book of resting LIMITs
re-checked on every tick — a fill decision it delegates to the injected
``FillModel``, then emits the raw ``FillReport``/``OrderStatusReport`` on the bus.
LIMIT semantics: a marketable order fills on arrival; ``post_only`` that would
cross is rejected; an unfilled IOC is cancelled on receipt; a GTC rests (LIVE)
until a later tick crosses it. ``cancel`` lifts a resting order off the book. It
owns no saga — the ``ExecutionManager`` turns these raw facts into canonical
``OrderEvent``s (ADR-0015). Frictionless in v1: price and quantity only, no
fees/margin/PnL (ADR-0013).
"""

from collections.abc import Mapping

from tickwright.domain import (
    Clock,
    EventBus,
    FillReport,
    InstrumentSpec,
    InvariantViolation,
    MarketTick,
    OrderState,
    OrderStatusReport,
    OrderType,
    PlaceOrder,
    Side,
    TimeInForce,
    VenueOrderView,
)

from .fill_model import Fill, FillModel


class PaperExchange:
    """An ``Exchange`` that fills against replayed/live ticks, deterministically."""

    def __init__(
        self,
        *,
        bus: EventBus,
        clock: Clock,
        fill_model: FillModel,
        instrument_specs: Mapping[str, InstrumentSpec] | None = None,
    ) -> None:
        self._bus = bus
        self._clock = clock
        self._fill_model = fill_model
        # Config-sourced venue metadata (ADR-0031). The exchange owns venue
        # knowledge, so min-notional a MARKET can only be judged at its fill
        # price is enforced here; the Engine also reads these to wire the guard.
        self._specs = dict(instrument_specs or {})
        self._latest_tick: dict[str, MarketTick] = {}
        self._fill_counts: dict[str, int] = {}
        self._book: dict[str, PlaceOrder] = {}  # resting LIMITs, keyed by cloid.
        # The venue's own memory, per cloid: the last status and every fill it
        # reported. This is what ``fetch_order`` answers reconciliation from —
        # a real venue remembers the orders it saw; so does the paper one.
        self._statuses: dict[str, OrderStatusReport] = {}
        self._fills: dict[str, list[FillReport]] = {}

    async def on_tick(self, tick: MarketTick) -> None:
        # Cache the latest price per symbol; MARKET fills read it (ADR-0027).
        self._latest_tick[tick.symbol] = tick
        await self._match_book(tick)

    async def _match_book(self, tick: MarketTick) -> None:
        # Re-check resting LIMITs for this symbol: any the tick now crosses fills.
        crossed = [
            order
            for order in self._book.values()
            if order.symbol == tick.symbol and self._crosses(order, tick)
        ]
        for order in crossed:
            del self._book[order.cloid]
            fill = self._fill_model.limit_fill(order, tick)
            await self._bus.publish(self._fill_report(order, fill))

    async def place(self, order: PlaceOrder) -> None:
        if order.order_type is OrderType.MARKET:
            await self._place_market(order)
        else:
            await self._place_limit(order)

    async def _place_market(self, order: PlaceOrder) -> None:
        tick = self._latest_tick.get(order.symbol)
        if tick is None:
            raise ValueError(f"no market tick cached for {order.symbol!r}; cannot fill MARKET")

        spec = self._specs.get(order.symbol)
        if spec is not None and tick.price * order.quantity < spec.min_notional:
            # Only the venue knows a MARKET's fill price, so it is the one that
            # can judge min-notional (ADR-0017): a too-small order is REJECTED
            # (sent, venue-adjudicated), the twin of the guard's LIMIT DENIED.
            await self._bus.publish(
                self._status_report(order, OrderState.REJECTED, reason="below min notional")
            )
            return

        fill = self._fill_model.market_fill(order, tick)
        await self._bus.publish(self._fill_report(order, fill))

    async def _place_limit(self, order: PlaceOrder) -> None:
        tick = self._latest_tick.get(order.symbol)
        if tick is None:
            raise ValueError(f"no market tick cached for {order.symbol!r}; cannot price LIMIT")

        if self._crosses(order, tick):
            if order.post_only:
                # post_only is a maker-only guarantee: crossing on arrival would
                # take liquidity, so the venue refuses it rather than filling.
                await self._bus.publish(
                    self._status_report(
                        order, OrderState.REJECTED, reason="post_only order would cross"
                    )
                )
                return
            # Marketable on arrival: fill now at the limit price, never rest.
            fill = self._fill_model.limit_fill(order, tick)
            await self._bus.publish(self._fill_report(order, fill))
            return

        if order.time_in_force is TimeInForce.IOC:
            # IOC never rests: an unfilled remainder is cancelled on receipt.
            await self._bus.publish(self._status_report(order, OrderState.CANCELLED))
            return

        # Not marketable on arrival: rest on the book and report it working (LIVE).
        # A later tick that crosses it fills it (see ``on_tick``).
        self._book[order.cloid] = order
        await self._bus.publish(self._status_report(order, OrderState.LIVE))

    async def cancel(self, cloid: str) -> None:
        order = self._book.pop(cloid, None)
        if order is None:
            # Nothing resting under this cloid: already filled/cancelled or never
            # placed. A benign no-op — the venue has nothing to report (ADR-0026).
            return
        await self._bus.publish(self._status_report(order, OrderState.CANCELLED))

    def _crosses(self, order: PlaceOrder, tick: MarketTick) -> bool:
        """Whether a trade at ``tick.price`` matches ``order``'s LIMIT price.

        A BUY fills when the market trades at or below its limit; a SELL when the
        market trades at or above it. ``price`` is always set for a LIMIT.
        """
        if order.price is None:
            # Only LIMITs reach the book; a priceless one is a broken assumption.
            raise InvariantViolation(f"LIMIT order {order.cloid} on the book with no price")
        if order.side is Side.BUY:
            return tick.price <= order.price
        return tick.price >= order.price

    def instrument_specs(self) -> Mapping[str, InstrumentSpec]:
        """The config-sourced per-symbol specs (ADR-0031), for the Engine to wire
        into the guard. A copy, so a caller can never mutate the venue's config."""
        return dict(self._specs)

    async def fetch_order(self, cloid: str) -> VenueOrderView | None:
        """Venue truth for ``cloid``: last reported status plus every fill.

        In-process reads cannot fail, so this never returns ``None`` — the
        startup reconciliation barrier always clears on paper (ADR-0024). An
        unknown cloid gets an empty view: positive proof of no record.
        """
        return VenueOrderView(
            status=self._statuses.get(cloid),
            fills=tuple(self._fills.get(cloid, [])),
        )

    def _status_report(
        self, order: PlaceOrder, status: OrderState, *, reason: str | None = None
    ) -> OrderStatusReport:
        now = self._clock.timestamp_ns()
        report = OrderStatusReport(
            ts_event=now,
            ts_init=now,
            cloid=order.cloid,
            symbol=order.symbol,
            status=status,
            reason=reason,
        )
        self._statuses[order.cloid] = report
        return report

    def _fill_report(self, order: PlaceOrder, fill: Fill) -> FillReport:
        index = self._fill_counts.get(order.cloid, 0) + 1
        self._fill_counts[order.cloid] = index
        now = self._clock.timestamp_ns()
        report = FillReport(
            ts_event=now,
            ts_init=now,
            cloid=order.cloid,
            symbol=order.symbol,
            trade_id=f"{order.cloid}-{index}",
            quantity=fill.quantity,
            price=fill.price,
        )
        self._fills.setdefault(order.cloid, []).append(report)
        return report
