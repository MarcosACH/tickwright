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
from decimal import Decimal

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
    below_min_notional,
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
        # Unfilled size still working per resting cloid — the fill model may
        # partial-fill a crossing LIMIT, so the venue tracks the remainder and
        # re-rests it until a later tick fills the rest (ADR-0012).
        self._remaining: dict[str, Decimal] = {}
        # The venue's own memory, per cloid: the last status and every fill it
        # reported. This is what ``fetch_order`` answers reconciliation from —
        # a real venue remembers the orders it saw; so does the paper one.
        self._statuses: dict[str, OrderStatusReport] = {}
        self._fills: dict[str, list[FillReport]] = {}
        # Filling off the tick stream *is* what a paper venue is (ADR-0012), so
        # it wires its own tick subscription here rather than leaving a line for
        # the composition root and every test to repeat (and be able to forget).
        # A real venue would not — it fills off its own matching engine, not our
        # replayed ticks — which is exactly why this lives in the paper adapter
        # and not on the ``Exchange`` seam. Safe against ordering: the feed
        # starts last (ADR-0024), so no tick is ever published before this.
        bus.subscribe(MarketTick, self.on_tick)

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
            if await self._fill_crossing_limit(order, tick):
                del self._book[order.cloid]  # fully filled: lift it off the book.

    async def _fill_crossing_limit(self, order: PlaceOrder, tick: MarketTick) -> bool:
        """Fill a crossing LIMIT per the model, capping to its working remainder.

        Returns ``True`` once the order is fully filled (and forgets its
        remainder); a ``None`` decision (queue miss) or a partial returns
        ``False``, leaving the reduced remainder recorded for a later tick. Book
        membership is the caller's to manage — a resting order and a marketable
        arrival share the fill math but differ on where the remainder lives.
        """
        fill = await self._fill_model.limit_fill(order, tick)
        if fill is None:
            return False  # queue miss (ADR-0012): nothing fills this tick.
        remaining = self._remaining.get(order.cloid, order.quantity)
        quantity = min(fill.quantity, remaining)
        await self._bus.publish(self._fill_report(order, Fill(quantity=quantity, price=fill.price)))
        remaining -= quantity
        if remaining <= 0:
            self._remaining.pop(order.cloid, None)
            return True
        self._remaining[order.cloid] = remaining
        return False

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
        if spec is not None and below_min_notional(tick.price, order.quantity, spec):
            # Only the venue knows a MARKET's fill price, so it is the one that
            # can judge min-notional (ADR-0017): a too-small order is REJECTED
            # (sent, venue-adjudicated), the twin of the guard's LIMIT DENIED.
            await self._bus.publish(
                self._status_report(order, OrderState.REJECTED, reason="below min notional")
            )
            return

        fill = await self._fill_model.market_fill(order, tick)
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
            # Marketable on arrival. The model may only partial-fill it, so the
            # remainder is handled exactly like a resting order's: GTC rests it,
            # IOC cancels it. A full fill on arrival never rests.
            if await self._fill_crossing_limit(order, tick):
                return
            if order.time_in_force is TimeInForce.IOC:
                # IOC never rests: cancel whatever remainder didn't fill now.
                self._remaining.pop(order.cloid, None)
                await self._bus.publish(self._status_report(order, OrderState.CANCELLED))
                return
            # GTC: rest the remainder for a later crossing tick. Announce it
            # working only if *nothing* filled (a queue miss); a partial already
            # drove the saga to PARTIALLY_FILLED, itself a working state.
            self._book[order.cloid] = order
            if self._remaining.get(order.cloid, order.quantity) == order.quantity:
                await self._bus.publish(self._status_report(order, OrderState.LIVE))
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
