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
``OrderEvent``s (ADR-0015).

It also stamps each fill's signed **fee**, the one economic figure it computes
rather than observes: the matching semantics above already say which side
provided liquidity, so the maker/taker bit is read off them and never configured
(ADR-0036). Margin and PnL remain deferred (ADR-0013).

**Funding** is the second thing it produces rather than observes, and the first
that arrives on no carrier: ``start()`` spawns the boundary generator and
``stop()`` cancels it (ADR-0037, ADR-0044 §7). The loop lives in ``funding.py``;
what stays here is the pricing — the last tick and the instrument's rate — while
the *size* each accrual is computed from is read from the ledger through the
injected ``account_net``. So this venue **holds no position state at all**,
which is ADR-0043 §4 verbatim rather than narrowed: after a crash it reports
nothing, the ``Store`` remains the sole authority for the paper ledger, and
``fetch_account_state`` still has no account truth to answer with.
"""

import asyncio
from collections.abc import Callable, Mapping
from decimal import Decimal

from tickwright.domain import (
    AccountSpec,
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
    VenueAccountState,
    VenueOrderView,
    VenueReadFailure,
    below_min_notional,
    fill_fee,
)

from .book import RestingBook
from .config import DEFAULT_ACCOUNT_LABEL
from .fill_model import Fill, FillModel
from .funding import HOUR_NS, FundingBasis, cancel, run_funding


class PaperExchange:
    """An ``Exchange`` that fills against replayed/live ticks, deterministically."""

    def __init__(
        self,
        *,
        bus: EventBus,
        clock: Clock,
        fill_model: FillModel,
        genesis_collateral: Decimal,
        account_label: str = DEFAULT_ACCOUNT_LABEL,
        account_net: Callable[[], Mapping[str, Decimal]],
        instrument_specs: Mapping[str, InstrumentSpec] | None = None,
        funding_interval_ns: int = HOUR_NS,
    ) -> None:
        self._bus = bus
        self._clock = clock
        self._fill_model = fill_model
        # Where this venue's funding notional comes from: the **account net
        # size** per symbol, read from the ledger that owns it rather than
        # tallied here (ADR-0034's Σ-invariant, ADR-0043 §4). Required, and for
        # the same reason ``genesis_collateral`` is — a venue defaulted to
        # holding nothing is not a neutral default but a silent decision to
        # charge no funding, which is indistinguishable from a correct run right
        # up until it isn't.
        self._account_net = account_net
        self._funding_interval_ns = funding_interval_ns
        self._funding_task: asyncio.Task[None] | None = None
        # The account's opening cash is the operator's declaration, never the
        # venue's: the paper exchange has nobody to ask, and the engine supplies
        # no collateral of its own (ADR-0042 §1). Required rather than defaulted
        # for exactly that reason — a plausible-looking number nobody chose is
        # the silent fiction the whole surface refuses everywhere else.
        self._account_spec = AccountSpec(
            account_id=f"paper-{account_label}",
            genesis_collateral=genesis_collateral,
        )
        # Config-sourced venue metadata (ADR-0031). The exchange owns venue
        # knowledge, so min-notional a MARKET can only be judged at its fill
        # price is enforced here; the Engine also reads these to wire the guard.
        self._specs = dict(instrument_specs or {})
        self._latest_tick: dict[str, MarketTick] = {}
        self._fill_counts: dict[str, int] = {}
        # Resting LIMITs and their working remainders. The fill model may
        # partial-fill a crossing LIMIT, so the book caps each fill to what is
        # still working and lifts the order off once it converges (ADR-0012).
        self._book = RestingBook()
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

    async def start(self) -> None:
        """Nothing to connect — the venue is in-process and its one link, the
        tick subscription, is wired at construction — but one loop to spawn.

        The funding generator is what this venue *runs* rather than what it is
        connected to (ADR-0037): a real venue pushes funding at its own
        boundaries, and paper has nobody to ask, so it settles them itself off
        the injected clock. Spawned here rather than at construction so that a
        venue built but never started publishes nothing, and so the loop's life
        is exactly the seam's declared ``start``/``stop`` pair.
        """
        self._funding_task = asyncio.create_task(
            run_funding(
                bus=self._bus,
                clock=self._clock,
                account_id=self._account_spec.account_id,
                basis=self._funding_basis,
                interval_ns=self._funding_interval_ns,
            )
        )

    async def stop(self) -> None:
        """Cancel the funding generator — the one thing this venue holds open.

        This is the teardown ADR-0044 §7 left undeclared "until there is
        teardown to do". Driven ahead of the bus drain deliberately: a generator
        that outlived this call would keep publishing into that drain and keep
        raising its high-water mark, so the cascade would never reach quiescence
        and a graceful stop would never finish.

        Safe on a venue that never started and safe on a second call, both
        because the runner's faulted teardown re-walks its whole membership from
        the top (ADR-0024). A generator that died of a refused ledger write is
        reported here rather than reaped quietly, so that re-walk is exactly
        where the reference has to be released **before** the wait rather than
        after it: kept until ``cancel`` returned, a surfaced failure would leave
        the dead task in place for the second pass to report all over again,
        turning one refusal into a fault recorded twice.
        """
        task, self._funding_task = self._funding_task, None
        await cancel(task)

    def _funding_basis(self) -> tuple[FundingBasis, ...]:
        """What each held symbol's accrual is computed from, as of now.

        The size is the **account net** the ledger reports; the price and the
        spec are the venue's own. That split is the decision: a paper venue is
        in-process and non-durable, so a size it tallied itself would survive
        exactly as long as the process — and a position outlives processes.
        Tallying here would put a second, unpersisted answer to "how much of
        this symbol is held" beside the durable one, and every way the two can
        drift is a wrong number nobody is watching: after a restart the tally
        reads flat while the ledger holds a position, and after a restart
        *followed by a trade* it reads that trade alone, which for a partial
        close is the wrong **sign**.

        Reading it per span rather than seeding it once is what makes that
        structural instead of merely fixed: there is no second copy to keep in
        step, so no path — recovery, a reconciliation heal, an absorbed
        unattributed fill — can move the ledger without moving this.

        A flat symbol is **not** filtered out: it produces a zero, and the
        generator's single "a zero accrues nothing" rule then covers both the
        flat position and the default ``funding_rate`` of ``0`` — one rule for
        one fact, rather than the same skip expressed twice in two places that
        could disagree.

        A symbol with no cached tick is genuinely absent rather than
        zero-priced: paper cannot price what it has never seen trade. That is
        reachable now in a way it was not when the size was local — a recovered
        position is held before this life has seen a single tick in its symbol —
        and it is still the right answer, since the alternative is to invent a
        price for a boundary. The next tick restores pricing, and the boundaries
        crossed before it are the restart gap the loop already skips
        (ADR-0043 §5.1).
        """
        return tuple(
            FundingBasis(
                symbol=symbol,
                signed_size=signed_size,
                price=tick.price,
                spec=self._specs[symbol],
            )
            for symbol, signed_size in self._account_net().items()
            if (tick := self._latest_tick.get(symbol)) is not None and symbol in self._specs
        )

    async def on_tick(self, tick: MarketTick) -> None:
        # Cache the latest price per symbol; MARKET fills read it (ADR-0027).
        self._latest_tick[tick.symbol] = tick
        await self._match_book(tick)

    async def _match_book(self, tick: MarketTick) -> None:
        # Re-check resting LIMITs for this symbol: any the tick now crosses fills.
        # The book lifts a fully-filled order off itself, so a partial just stays.
        for order in self._book.resting():
            if order.symbol == tick.symbol and self._crosses(order, tick):
                # Off the book on a later tick: this order was the resting side,
                # so it *made* liquidity (ADR-0036). ``post_only`` reaches a fill
                # only down this path — one that would have crossed on arrival is
                # already rejected — so its maker-only guarantee is structural
                # here rather than a flag anyone has to re-read.
                await self._fill_crossing_limit(order, tick, maker=True)

    async def _fill_crossing_limit(
        self, order: PlaceOrder, tick: MarketTick, *, maker: bool
    ) -> bool:
        """Fill a crossing LIMIT per the model against its working remainder.

        Returns ``True`` once the order is fully filled. A ``None`` decision (a
        queue miss) or a partial returns ``False``; the book keeps the reduced
        remainder resting for a later tick. The order must already be on the
        book — the book caps the fill and lifts it off on completion.

        ``maker`` rides in from the caller because this is the one path both
        boundaries share and neither the order nor the book records which it
        came down: an arrival that crosses took liquidity, a later tick lifting a
        resting order provided it. The bit is consumed here and not stored — it
        selects the rate, and no v1 consumer reads it back (ADR-0036).
        """
        fill = await self._fill_model.limit_fill(order, tick)
        if fill is None:
            return False  # queue miss (ADR-0012): nothing fills this tick.
        quantity, complete = self._book.apply_fill(order.cloid, fill.quantity)
        await self._bus.publish(
            self._fill_report(order, Fill(quantity=quantity, price=fill.price), maker=maker)
        )
        return complete

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
        # A MARKET fills on arrival against the price the venue already holds: it
        # takes liquidity by definition, so there is no maker branch (ADR-0036).
        await self._bus.publish(self._fill_report(order, fill, maker=False))

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
            # Marketable on arrival. Rest it first so the book owns its
            # remainder, then fill: a full fill lifts it right back off; the
            # model may only partial-fill, and the remainder is then handled
            # exactly like a resting order's — GTC keeps it, IOC cancels it.
            self._book.rest(order)
            # Marketable on arrival: it crossed the moment it landed, so this
            # fill took liquidity exactly as a MARKET's does (ADR-0036). A
            # remainder that survives to a later tick is a *different* fill and
            # comes back down ``_match_book`` as a maker.
            if await self._fill_crossing_limit(order, tick, maker=False):
                return  # fully filled on arrival: the book already lifted it off.
            if order.time_in_force is TimeInForce.IOC:
                # IOC never rests: drop whatever remainder didn't fill now.
                self._book.remove(order.cloid)
                await self._bus.publish(self._status_report(order, OrderState.CANCELLED))
                return
            # GTC keeps the remainder resting for a later crossing tick. Announce
            # it working only if *nothing* filled (a queue miss); a partial
            # already drove the saga to PARTIALLY_FILLED, itself a working state.
            if not self._book.has_partial(order.cloid):
                await self._bus.publish(self._status_report(order, OrderState.LIVE))
            return

        if order.time_in_force is TimeInForce.IOC:
            # IOC never rests: an unfilled remainder is cancelled on receipt.
            await self._bus.publish(self._status_report(order, OrderState.CANCELLED))
            return

        # Not marketable on arrival: rest on the book and report it working (LIVE).
        # A later tick that crosses it fills it (see ``on_tick``).
        self._book.rest(order)
        await self._bus.publish(self._status_report(order, OrderState.LIVE))

    async def cancel(self, cloid: str) -> None:
        order = self._book.remove(cloid)
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

    def account_spec(self) -> AccountSpec:
        """The paper account's static declaration (ADR-0038/0042).

        ``paper-<label>`` is deliberately **two** segments where a live venue's
        id is three (venue + network + address), and the label's slug
        constraint is what keeps that distinction unambiguous to a reader and to
        a parser. Netting is ``NET`` — the paper book fills one signed position
        per symbol, which is what the v1 model assumes throughout.
        """
        return self._account_spec

    async def fetch_order(self, cloid: str) -> VenueOrderView | VenueReadFailure:
        """Venue truth for ``cloid``: last reported status plus every fill.

        In-process reads cannot fail, so this never returns a
        ``VenueReadFailure`` — the startup reconciliation barrier always clears
        on paper (ADR-0024). An unknown cloid gets an empty view: positive proof
        of no record.
        """
        return VenueOrderView(
            status=self._statuses.get(cloid),
            fills=tuple(self._fills.get(cloid, [])),
        )

    async def fetch_account_state(self) -> VenueAccountState | None:
        """``None`` **always — by construction, not by failure**.

        Unlike ``fetch_order`` above, which answers an empty view because this
        venue genuinely knows every cloid it was asked to place, there is no
        account truth here to answer with at all: this venue holds resting
        orders, per-cloid fill reports and the latest tick, and no position, cash
        or equity state (ADR-0043 §4). The ledger lives in the engine, and
        reconciling it against itself would answer nothing.

        ``None`` is therefore the only value that stays fail-closed under every
        wiring, including a future one that mistakenly points the reconcile
        cadence at paper: it freezes and heals nothing (ADR-0011 inv 1). A
        zero-filled ``VenueAccountState`` would be fail-*open* — precisely the
        fabricated flat ADR-0034 forbids — and would heal a restored ledger down
        to flat. This is not the outage sentinel abused; it is the same contract,
        *no truth to compare against ⇒ never heal*, reached by a different route.
        """
        return None

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

    def _fill_report(self, order: PlaceOrder, fill: Fill, *, maker: bool) -> FillReport:
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
            fee=self._fee(order.symbol, fill, maker=maker),
        )
        self._fills.setdefault(order.cloid, []).append(report)
        return report

    def _fee(self, symbol: str, fill: Fill, *, maker: bool) -> Decimal:
        """This fill's signed fee, or zero for a symbol carrying no spec.

        Computed **after** matching, off the fill the model already decided, so
        the ``FillModel`` seam still emits price and quantity only and no money
        math enters the matching path — the principle ADR-0013 was protecting
        and ADR-0036 carries forward while lifting its fee deferral.

        An unspecced symbol charges nothing rather than raising: the venue is
        already tradable without a spec — min-notional is skipped the same way in
        ``_place_market`` — so a spec is what an operator adds to *model* venue
        friction, not a precondition for filling.
        """
        spec = self._specs.get(symbol)
        if spec is None:
            return Decimal("0")
        return fill_fee(price=fill.price, quantity=fill.quantity, maker=maker, spec=spec)
