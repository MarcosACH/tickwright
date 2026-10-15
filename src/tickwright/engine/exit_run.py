"""Operator exit runs (ADR-0052, ADR-0060): one job in place of the strategies.

The ``Engine`` boots and reconciles as usual, then runs an ``ExitJob`` instead of
starting its strategies. The job gets what it needs in an ``ExitContext``,
because the engine builds those parts itself and the composition root never
sees them.
"""

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from tickwright.domain import (
    OPERATOR,
    AccountAnchor,
    Clock,
    EventBus,
    ExternalOrderRef,
    InvariantViolation,
    MarketTick,
    MarkTick,
    OrderAnchor,
    OrderRef,
    OrderRefusal,
    OrderRefusedReport,
    OrderType,
    PreTradeGuard,
    Side,
    SignalEmitter,
    Store,
    StoreNeverUsed,
    TimeInForce,
    VenueOpenOrder,
    VenueReadFailure,
    derive_cloid,
)
from tickwright.observability import NamedEvent, named_event

from .checkpoint import Checkpointer
from .feed_handle import FeedHandle
from .reconcile import PassResult, Reconciler


class ExitStatus(StrEnum):
    """How a job ended. Only a final venue read can make it ``DONE`` (ADR-0060)."""

    DONE = "done"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True, kw_only=True)
class ExitOutcome:
    """What a job reports back to the engine when it returns."""

    status: ExitStatus
    left: str
    """What the last venue read left, or how that read failed, for ``exit.finished``.

    The job writes it, because only the job knows what its last read covered.
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class ExitContext:
    """The engine's own parts that a job may use, built after the barrier."""

    account: AccountAnchor
    orders: OrderAnchor
    checkpointer: Checkpointer
    guard: PreTradeGuard
    bus: EventBus
    clock: Clock
    feed: FeedHandle
    reconciler: Reconciler
    pass_interval_seconds: float
    """How long a job waits between its own reconcile passes. No cadence runs in an
    exit run, so the job paces them itself (ADR-0052)."""
    reconcile_account: Callable[[], Awaitable[object]] | None
    """One account reconcile pass, or ``None`` on a venue with no account truth.
    It heals a position the books cannot see into the unattributed partition
    (ADR-0038), so flatten can size a close against it (ADR-0054)."""


class ExitJob(Protocol):
    """One operator job the engine runs in place of its strategies."""

    @property
    def name(self) -> str:
        """The job's name in ``exit.finished``."""
        ...

    def admit(self, store: Store) -> StoreNeverUsed | None:
        """Return the refusal if the run must not boot on ``store``, else ``None``.

        It runs after the store lock and before ``recover()``, because boot
        writes the account row this check reads (ADR-0052). The engine reports
        a refusal, so a job cannot forget ``exit.refused``.
        """
        ...

    async def run(self, context: ExitContext) -> ExitOutcome:
        """Do the job and say what the venue holds at the end."""
        ...


def _admit_store(store: Store, *, new_store: bool) -> StoreNeverUsed | None:
    """Refuse a store no engine has used. That is almost always the wrong
    directory, and the engine the operator meant to stop may still trade there
    (ADR-0052). ``new_store`` is the operator saying the store is new on purpose."""
    if store.load_account() is not None:
        return None
    if new_store:
        named_event(
            NamedEvent.EXIT_NEW_STORE,
            store=store.name,
            warning="no engine has used this store: any engine trading elsewhere keeps trading",
        )
        return None
    return StoreNeverUsed(store.name)


class OperatorCancelAll:
    """``tickwright cancel-all``: cancel every order resting in the account."""

    name = "cancel_all"

    def __init__(self, *, new_store: bool = False) -> None:
        self._new_store = new_store

    def admit(self, store: Store) -> StoreNeverUsed | None:
        return _admit_store(store, new_store=self._new_store)

    async def run(self, context: ExitContext) -> ExitOutcome:
        left = await context.account.fetch_open_orders()
        # One cancel, then one more for whatever the read still shows (ADR-0060).
        for _ in range(2):
            left = await _cancel_round(left, context)
        if isinstance(left, VenueReadFailure):
            return ExitOutcome(status=ExitStatus.STOPPED, left=describe_failure(left))
        if left:
            named_event(NamedEvent.CANCEL_ALL_ORDERS_REMAIN, orders=describe_orders(left))
            return ExitOutcome(status=ExitStatus.STOPPED, left=describe_orders(left))
        return ExitOutcome(status=ExitStatus.DONE, left=NOTHING_LEFT)


class Flatten:
    """``tickwright flatten``: close every position the account holds.

    ``feed_ends`` is true for a replay. The composition root sets it, because
    only the root knows which feed it built (ADR-0059).
    """

    name = "flatten"

    def __init__(self, *, feed_ends: bool, new_store: bool = False) -> None:
        self._feed_ends = feed_ends
        self._new_store = new_store

    def admit(self, store: Store) -> StoreNeverUsed | None:
        return _admit_store(store, new_store=self._new_store)

    async def run(self, context: ExitContext) -> ExitOutcome:
        # First, so a crash mid-flatten still leaves the store halted (ADR-0053).
        context.guard.trip_kill_switch("flatten: operator exit")
        # A guard with no kill switch reads untripped right after the trip.
        if not context.guard.kill_switch_tripped:
            named_event(
                NamedEvent.EXIT_UNGUARDED,
                warning="no kill switch under GUARD=noop: the engine trades as soon as it restarts",
            )
        # A resting order could fill after the close and reopen exposure. So the
        # cancel all step runs first, and flatten stops where it stops (ADR-0060).
        cancelled = await OperatorCancelAll().run(context)
        if cancelled.status is ExitStatus.STOPPED:
            return cancelled
        positions = await context.account.fetch_positions()
        if isinstance(positions, VenueReadFailure):
            return ExitOutcome(status=ExitStatus.STOPPED, left=describe_failure(positions))
        # A symbol the venue already reads flat gets no loop, so no later read would
        # move what the strategies still hold. It moves here instead (ADR-0054).
        for symbol in sorted(context.checkpointer.portfolio.account_net()):
            if not positions.get(symbol):
                context.checkpointer.checkpoint_leftover(symbol)
        if positions:
            priced = _Prices(positions, waits=not self._feed_ends, clock=context.clock)
            # Before the feed starts, so no tick goes unseen.
            context.bus.subscribe(MarketTick, priced.note_trade)
            context.bus.subscribe(MarkTick, priced.note_mark)
            await context.feed.start(positions)
            # A replay plays the whole file first, so each close fills at the
            # last row (ADR-0059). A live feed never ends. Each loop waits for
            # its own symbol's first price instead.
            if self._feed_ends:
                await context.feed.ended()
            emitter = SignalEmitter(
                strategy_id=OPERATOR,
                bus=context.bus,
                clock=context.clock,
                record_seq=context.checkpointer.raise_seq,
            )
            # Seeded from the store high-water, as a strategy's is. An earlier flatten's seq
            # is consumed, and the manager drops a signal id it has seen (ADR-0006).
            high_water = context.checkpointer.cache.seq_high_water()
            emitter.set_next_seq(high_water.get(OPERATOR, 0) + 1)
            turns = _Turns(context.reconciler, context.reconcile_account)
            context.bus.subscribe(OrderRefusedReport, turns.note_refusal)
            # One loop per symbol, all at the same time, so a thin book on one
            # symbol does not hold back the others (ADR-0056). A task group
            # cancels the rest when one faults. A loop reports nothing, because
            # only the final read can say what is left (ADR-0060).
            async with asyncio.TaskGroup() as group:
                for symbol, size in positions.items():
                    group.create_task(
                        _flatten_symbol(symbol, size, priced, emitter, turns, context)
                    )
        return await _final_read(context)


DRY_ATTEMPTS_TO_GIVE_UP = 3
"""Dry attempts in a row that stop a symbol's loop (ADR-0056)."""

DRY_WAIT_SECONDS = 2.0
"""The wait after a dry attempt, before the next one (ADR-0056)."""

RATE_LIMIT_WAIT_SECONDS = 10.0
"""The wait after the venue refused a close or a read for a rate limit (ADR-0056, ADR-0060)."""

_RATE_LIMITS = {OrderRefusal.RATE_LIMITED_ADDRESS: "address", OrderRefusal.RATE_LIMITED_IP: "ip"}
"""The ``limit`` field ``flatten.rate_limited`` names for each rate limit (ADR-0060)."""


class _Prices:
    """Which held symbols the feed has priced, for one flatten run.

    A symbol is priced once it has both a trade and a mark. A market order is
    priced from the trade and capped by the mark, so either one missing makes
    the venue refuse to price it (ADR-0059).

    On a live feed, an attempt waits up to one dry wait for its symbol's first
    price. On subscribe the venue sends it at once, so a healthy run counts no
    dry attempt. A dead socket still ends in the give-up rule, never a hang
    (ADR-0059). After a replay no price can come, so nothing waits.
    """

    def __init__(self, symbols: Iterable[str], *, waits: bool, clock: Clock) -> None:
        self._seen = {symbol: asyncio.Event() for symbol in symbols}
        self._traded: set[str] = set()
        self._marked: set[str] = set()
        self._waits = waits
        self._clock = clock

    async def note_trade(self, tick: MarketTick) -> None:
        self._traded.add(tick.symbol)
        self._note(tick.symbol)

    async def note_mark(self, mark: MarkTick) -> None:
        self._marked.add(mark.symbol)
        self._note(mark.symbol)

    def _note(self, symbol: str) -> None:
        seen = self._seen.get(symbol)
        if seen is not None and symbol in self._traded and symbol in self._marked:
            seen.set()

    async def has(self, symbol: str) -> bool:
        seen = self._seen[symbol]
        if self._waits and not seen.is_set():
            price = asyncio.ensure_future(seen.wait())
            timer = asyncio.ensure_future(self._clock.sleep(DRY_WAIT_SECONDS))
            try:
                await asyncio.wait((price, timer), return_when=asyncio.FIRST_COMPLETED)
            finally:
                # Also on a stop mid-wait, so neither task outlives the loop.
                price.cancel()
                timer.cancel()
        return seen.is_set()


class _Turns:
    """One close or reconcile pass at a time, shared by every loop of one flatten run.

    A loop that needs a turn waits for the one in progress, then takes its own.
    A pass that started before the loop's attempt ended may not cover it.

    A close takes a turn too. A publish made while another task's cascade runs
    joins that cascade and returns before it is handled. The loop would then
    find no saga, read it as a close that filled nothing, and send a second
    one beside it (ADR-0056).

    An account pass takes a turn too. Beside a close, it could read a fill the
    venue made before the engine booked it. It would heal that fill into the
    books, and the real fill would then book it again.
    """

    def __init__(
        self, reconciler: Reconciler, reconcile_account: Callable[[], Awaitable[object]] | None
    ) -> None:
        self._reconciler = reconciler
        self._reconcile_account = reconcile_account
        self._lock = asyncio.Lock()
        self._refusals: dict[str, OrderRefusal] = {}

    async def note_refusal(self, report: OrderRefusedReport) -> None:
        self._refusals[report.cloid] = report.refusal

    async def account_pass(self) -> None:
        """Run one account pass, on a venue that has account truth."""
        if self._reconcile_account is None:
            return
        async with self._lock:
            await self._reconcile_account()

    async def run_pass(self) -> PassResult:
        """Run both halves of one pass. The second runs only when the first proved."""
        async with self._lock:
            inflight = await self._reconciler.reconcile_inflight()
            if inflight is not PassResult.PROVED:
                return inflight
            return await self._reconciler.reconcile_open_orders()

    async def close(
        self, symbol: str, size: Decimal, emitter: SignalEmitter
    ) -> tuple[str, OrderRefusal | None]:
        """Send one close, and return once the engine has handled it.

        Returns its cloid, and why the venue did not take it, if it did not.
        The venue's answer is part of the handling, so it is known by then.
        """
        async with self._lock:
            cloid = await _close(symbol, size, emitter)
            return cloid, self._refusals.pop(cloid, None)


class _DryAttempts:
    """What a step that closed nothing costs one symbol's loop (ADR-0056, ADR-0060).

    Most steps count as one dry attempt in a row, then wait 2 seconds. A rate
    limit waits 10 seconds and does not count, because waiting it out makes
    progress. This is the one place that tells the two apart.
    """

    def __init__(self, symbol: str, clock: Clock) -> None:
        self._symbol = symbol
        self._clock = clock
        self._count = 0

    def reset(self) -> None:
        self._count = 0

    async def gave_up(self, reason: str, size: Decimal) -> bool:
        """Count one dry attempt. Say whether it was the last, or wait before the next.

        ``size`` is the last size the venue reported. After failed reads it can be
        from before the last close, so the final read says what is left (ADR-0060).
        """
        self._count += 1
        named_event(
            NamedEvent.FLATTEN_DRY_ATTEMPT, symbol=self._symbol, count=self._count, reason=reason
        )
        if self._count == DRY_ATTEMPTS_TO_GIVE_UP:
            named_event(NamedEvent.FLATTEN_GAVE_UP, symbol=self._symbol, size=str(size))
            return True
        await self._clock.sleep(DRY_WAIT_SECONDS)
        return False

    async def read_failed(self, size: Decimal, *, rate_limited: bool) -> bool:
        """Count a failed venue read as dry, unless the IP limit refused it.

        Says whether the loop gave up, as ``gave_up`` does. Only the IP limit
        covers reads, so that is the limit the wait names.
        """
        if rate_limited:
            await self.wait_out("ip")
            return False
        return await self.gave_up("read_failed", size)

    async def wait_out(self, limit: str) -> None:
        """Wait out a rate limit. ``limit`` is the field ``flatten.rate_limited`` names."""
        named_event(NamedEvent.FLATTEN_RATE_LIMITED, symbol=self._symbol, limit=limit)
        await self._clock.sleep(RATE_LIMIT_WAIT_SECONDS)


async def _flatten_symbol(
    symbol: str,
    size: Decimal,
    priced: _Prices,
    emitter: SignalEmitter,
    turns: _Turns,
    context: ExitContext,
) -> None:
    """One symbol's loop: wait for a close the boot left open, then close the rest.

    It closes until the venue reads the symbol flat, or stops after 3 dry
    attempts in a row. A failed read is dry too, so a venue that cannot answer
    never hangs the run (ADR-0060). A rate limit is the one failure that waits
    instead. The final read reports what is left.
    """
    # One count for the whole loop, so the resume wait's dry attempts are in the row.
    dry = _DryAttempts(symbol, context.clock)
    left = await _resume_closes(symbol, size, dry, turns, context)
    while left is not None:
        left = await _attempt(symbol, left, priced, emitter, dry, turns, context)


async def _attempt(
    symbol: str,
    size: Decimal,
    priced: _Prices,
    emitter: SignalEmitter,
    dry: _DryAttempts,
    turns: _Turns,
    context: ExitContext,
) -> Decimal | None:
    """Try to close ``size`` once, then return what is left. ``None`` ends the loop.

    Every attempt reads the venue before it counts. So a venue that went flat
    ends the loop with no dry attempt, even when no close went out (ADR-0056).
    A fill resets the count, so a thin book that keeps filling a little never
    gives up.
    """
    # Why the attempt closed nothing. It stays ``None`` when the close filled,
    # or when it waited out a rate limit, because waiting makes progress.
    reason: str | None = None
    # No venue can send a market close without a price.
    if not await priced.has(symbol):
        reason = "no_price"
    elif await _size_gap(symbol, size, turns, context):
        reason = "size_gap"
    else:
        cloid, refusal = await turns.close(symbol, size, emitter)
        limit = _RATE_LIMITS.get(refusal) if refusal is not None else None
        verdict = _verdict(cloid, size, dry, turns, context)
        if limit is not None:
            # The 10 seconds count from the refusal, so they run beside the verdict.
            decided = await _beside(verdict, dry.wait_out(limit))
        else:
            decided = await verdict
        if not decided:
            return None
        if _filled(cloid, context):
            # Before the read, so a failed read after a fill starts a new row.
            dry.reset()
        elif limit is None:
            # A send that dies counts as refused too (ADR-0060).
            reason = "no_fill" if refusal is None else "refused"
    left = await _read_left(symbol, size, dry, context)
    if left is None:
        return None
    if reason is not None and await dry.gave_up(reason, left):
        return None
    return left


async def _beside(verdict: Coroutine[None, None, bool], wait: Coroutine[None, None, None]) -> bool:
    """Run ``verdict`` and ``wait`` at the same time. Return once both are done."""
    async with asyncio.TaskGroup() as group:
        decided = group.create_task(verdict)
        group.create_task(wait)
    return decided.result()


async def _size_gap(symbol: str, size: Decimal, turns: _Turns, context: ExitContext) -> bool:
    """Whether the account net size on ``symbol`` differs from the venue's ``size``.

    A close is split across the partitions it closes (ADR-0054). Against a gap,
    some of it would land on no partition, or one would be left holding size.
    The gap can be fills the bus has not delivered yet, so a drain comes first.
    On a venue with account truth it can also be a trade made by hand, which
    only an account pass can see.
    """
    if _held(symbol, context) == size:
        return False
    await context.bus.drain()
    if _held(symbol, context) == size:
        return False
    await turns.account_pass()
    return _held(symbol, context) != size


def _held(symbol: str, context: ExitContext) -> Decimal:
    """The account net size on ``symbol``, from the portfolio's one definition of it."""
    return context.checkpointer.portfolio.account_net().get(symbol, Decimal(0))


async def _verdict(
    cloid: str, size: Decimal, dry: _DryAttempts, turns: _Turns, context: ExitContext
) -> bool:
    """Run reconcile passes until the close's saga is terminal (ADR-0011, ADR-0026).

    A pass covers both halves. ``reconcile_inflight`` resolves a send that never
    acked. ``reconcile_open_orders`` resolves an acked order whose last push was lost.

    A failed pass costs what any failed read costs (``_DryAttempts.read_failed``),
    so a venue that cannot answer never hangs the run (ADR-0060). False when
    failed passes used up the dry attempts. A second close beside an open one
    could close the size twice. So a failed pass waits for this close again, and
    a give-up leaves it to the next boot.
    """
    while not _decided(cloid, context):
        await context.clock.sleep(context.pass_interval_seconds)
        result = await turns.run_pass()
        if result is not PassResult.PROVED and await dry.read_failed(
            size, rate_limited=result is PassResult.RATE_LIMITED
        ):
            return False
    return True


async def _read_left(
    symbol: str, held: Decimal, dry: _DryAttempts, context: ExitContext
) -> Decimal | None:
    """What the venue still holds on ``symbol``, read again after each failed read.

    ``None`` ends the loop. Either the failed reads used up the dry attempts, or
    the venue reads flat. ``held`` is the last size read, for ``flatten.gave_up``.

    A hand trade the books cannot see may have closed a flat venue, and no order
    can close what the venue does not hold. So what the strategies still hold
    moves to the unattributed partition (ADR-0054, ADR-0056).
    """
    while isinstance(positions := await context.account.fetch_positions(), VenueReadFailure):
        if await dry.read_failed(held, rate_limited=positions is VenueReadFailure.RATE_LIMITED_IP):
            return None
    left = positions.get(symbol, Decimal(0))
    if not left:
        context.checkpointer.checkpoint_leftover(symbol)
        return None
    return left


async def _resume_closes(
    symbol: str, size: Decimal, dry: _DryAttempts, turns: _Turns, context: ExitContext
) -> Decimal | None:
    """Wait for any close the boot left open in ``symbol``, then say what is left to close.

    That close could still fill, so a second one sent beside it could close the
    size twice. A symbol never has two closes open (ADR-0056). So the wait and the
    read are the same ones that follow an attempt. ``None`` ends the loop, as it
    does there. After a give-up, the final read reports the size, and the next boot
    resumes the saga. The wait runs after the feed has played, because its passes
    move the replay clock.
    """
    left_open = context.checkpointer.cache.open_orders(strategy_id=OPERATOR, symbol=symbol)
    if not left_open:
        return size
    # What each close had filled before this run, so only a fill during the wait counts.
    before = {order.cloid: order.cum_qty for order in left_open}
    for cloid in before:
        if not await _verdict(cloid, size, dry, turns, context):
            return None
    if any(_cum_qty(cloid, context) > qty for cloid, qty in before.items()):
        # A fill on a close from an earlier run ends the row too, before the read
        # (ADR-0056).
        dry.reset()
    # The old close may have filled during the wait, so only a new read sizes the next.
    return await _read_left(symbol, size, dry, context)


async def _close(symbol: str, size: Decimal, emitter: SignalEmitter) -> str:
    """Send one reduce-only market IOC order for the whole of ``size`` (ADR-0054).

    Returns the cloid of the order's saga.
    """
    signal_id = await emitter.place(
        symbol=symbol,
        side=Side.SELL if size > 0 else Side.BUY,
        quantity=abs(size),
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.IOC,
        reduce_only=True,
    )
    return derive_cloid(signal_id)


def _decided(cloid: str, context: ExitContext) -> bool:
    saga = context.checkpointer.cache.get_order(cloid)
    if saga is None:
        # The manager writes a saga for every signal it handles, a denial included.
        # So the close was never handled, and it may still reach the venue. A
        # second close beside it could close the size twice (ADR-0056).
        raise InvariantViolation(f"flatten close {cloid} was never handled")
    return saga.is_terminal


def _filled(cloid: str, context: ExitContext) -> bool:
    return _cum_qty(cloid, context) > 0


def _cum_qty(cloid: str, context: ExitContext) -> Decimal:
    saga = context.checkpointer.cache.get_order(cloid)
    return Decimal(0) if saga is None else saga.cum_qty


async def _final_read(context: ExitContext) -> ExitOutcome:
    """Done only when the venue holds no resting order and no position (ADR-0060)."""
    orders = await context.account.fetch_open_orders()
    if isinstance(orders, VenueReadFailure):
        return ExitOutcome(status=ExitStatus.STOPPED, left=describe_failure(orders))
    positions = await context.account.fetch_positions()
    if isinstance(positions, VenueReadFailure):
        return ExitOutcome(status=ExitStatus.STOPPED, left=describe_failure(positions))
    if orders or positions:
        left = [describe_orders(orders)] if orders else []
        left += [f"{symbol}={size}" for symbol, size in positions.items()]
        return ExitOutcome(status=ExitStatus.STOPPED, left=",".join(left))
    return ExitOutcome(status=ExitStatus.DONE, left=NOTHING_LEFT)


NOTHING_LEFT = "none"
"""``left`` when the last read found nothing to act on."""


def describe_failure(failure: VenueReadFailure) -> str:
    """``left`` when the last read failed."""
    return f"read_failed: {failure.value}"


def describe_orders(orders: Sequence[VenueOpenOrder | ExternalOrderRef]) -> str:
    """Name each order by its oid, or by its cloid where the venue gives no oid.

    One string, because every catalog field is a scalar.
    """
    return ",".join(
        order.venue_oid if isinstance(order, ExternalOrderRef) else order.venue_oid or order.cloid
        for order in orders
    )


async def _cancel_round(
    resting: list[VenueOpenOrder | ExternalOrderRef] | VenueReadFailure, context: ExitContext
) -> list[VenueOpenOrder | ExternalOrderRef] | VenueReadFailure:
    """Cancel what ``resting`` shows, then read again.

    A failed read or an empty book is returned as it is, with nothing sent. The
    cancel statuses are never read. Only the next read says what is left.
    """
    if isinstance(resting, VenueReadFailure) or not resting:
        return resting
    await context.orders.cancel([_mark(order, context.checkpointer) for order in resting])
    return await context.account.fetch_open_orders()


def _mark(
    order: VenueOpenOrder | ExternalOrderRef, checkpointer: Checkpointer
) -> OrderRef | ExternalOrderRef:
    """The ref to cancel ``order`` by, with its saga marked first if it has one.

    The mark is durable before the send. Without it, a crash after the send
    leaves reconcile to judge the order a ghost and mark it ``REJECTED``.
    """
    if isinstance(order, ExternalOrderRef):
        # The engine did not place it, so there is no saga to mark (ADR-0052).
        return order
    saga = checkpointer.cache.get_order(order.cloid)
    if saga is None:
        return OrderRef(cloid=order.cloid, symbol=order.symbol, venue_oid=order.venue_oid)
    saga.mark_operator_cancel(ts_ns=checkpointer.clock.timestamp_ns())
    checkpointer.checkpoint(saga)
    return OrderRef.of(saga)
