"""Paper's funding generator: the boundary loop on the injected ``Clock`` (ADR-0037).

Paper has no venue to push funding, so it **generates** accruals — on
``Clock.sleep_until``, the same pure waiter the reconcile cadences use, and
deliberately *not* on ``run_cadence``: those collapse a time-jump to one firing
because reconciling is convergent, while funding is **additive** and a jump
across N boundaries is N distinct payments.

The seam is the ``Exchange``: a real ``PaperExchange`` over a real ``InMemoryBus``
and a ``ManualClock``, driven by the venue's own verbs — ``run()`` supervised as
a task, exactly as the runner supervises it in its ``TaskGroup``. Nothing here
sleeps: virtual time is advanced by hand, which is the whole point of the
primitive.
"""

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from decimal import Decimal

import pytest
from ledgers import GENESIS

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.domain import (
    AggressorSide,
    Event,
    FundingAccrual,
    InstrumentSpec,
    InvariantViolation,
    MarketTick,
)

HOUR_NS = 3_600_000_000_000

# The instants below are named in wall-clock terms because the boundary rule is:
# an epoch-aligned multiple of the interval, which at one hour is the top of each
# UTC hour. 2024-01-01T00:00:00Z is 1 704 067 200 s since the epoch.
MIDNIGHT_NS = 1_704_067_200 * 1_000_000_000


def _at(hours: float) -> int:
    """``hours`` after 2024-01-01T00:00:00Z, in epoch ns."""
    return MIDNIGHT_NS + int(hours * HOUR_NS)


def _spec(funding_rate: str = "0") -> InstrumentSpec:
    return InstrumentSpec(
        symbol="BTC",
        sz_decimals=3,
        max_decimals=6,
        min_notional=Decimal("0"),
        funding_rate=Decimal(funding_rate),
    )


def _tick(price: str, ts: int, symbol: str = "BTC") -> MarketTick:
    return MarketTick(
        ts_event=ts,
        ts_init=ts,
        symbol=symbol,
        price=Decimal(price),
        size=Decimal("5"),
        aggressor_side=AggressorSide.BUY,
        trade_id=f"t{ts}",
        seq=0,
    )


def _venue(
    bus: InMemoryBus,
    clock: ManualClock,
    *,
    account_net: Callable[[], Mapping[str, Decimal]],
    funding_rate: str = "0.0001",
) -> PaperExchange:
    return PaperExchange(
        bus=bus,
        clock=clock,
        fill_model=ImmediateFillModel(),
        genesis_collateral=GENESIS,
        instrument_specs={"BTC": _spec(funding_rate)},
        account_net=account_net,
    )


async def _quiesce(times: int = 5) -> None:
    """Let the generator task run to quiescence after virtual time moves."""
    for _ in range(times):
        await asyncio.sleep(0)


@asynccontextmanager
async def _supervising(venue: PaperExchange) -> AsyncIterator[asyncio.Task[None]]:
    """Drive the venue's lifecycle the way the runner does (ADR-0024).

    ``start()`` connects nothing on this venue; the boundary loop is ``run()``,
    the seam's supervised long-lived half, so a case can only observe it by
    holding the task the way the runner's ``TaskGroup`` holds it — which is also
    the only way the loop's *failure* is reachable at all. The task is yielded
    for exactly that: a case about a refused write awaits it, and every other
    case ignores it and takes the runner's own cancel-and-wait on the way out.
    """
    await venue.start()
    task = asyncio.create_task(venue.run())
    try:
        yield task
    finally:
        await venue.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _drive(
    *,
    held: Mapping[str, Decimal] | None = None,
    through: float,
    funding_rate: str = "0.0001",
    start: float = 0.5,
    priced: bool = True,
) -> list[FundingAccrual]:
    """Open the venue at ``start`` holding ``held``, price it at 50 000, jump to ``through``.

    One driver for every case here, because the cases differ only in what is
    held, at what rate, how far time moved, and whether the venue has ever seen
    the symbol trade — and a boundary loop is testable *only* through those,
    which is the point of the injected clock.

    ``held`` is the **account net size** per symbol, handed to the venue the way
    the ledger hands it in production. It is stated rather than traded into
    existence, and that is the point: what the venue holds is not a consequence
    of what this process filled — a recovered position was filled by a process
    that is gone — so a driver that could only express holdings by placing
    orders could not express the ordinary case at all.

    ``priced=False`` withholds the opening tick, which is the *only* way to
    express a venue that holds something it cannot price. It became expressible
    at all when the size moved to the ledger: a venue tallying its own fills
    could never hold what it had not just seen trade.
    """
    bus = InMemoryBus()
    clock = ManualClock(start_ns=_at(start))
    accruals: list[FundingAccrual] = []

    async def collect(event: Event) -> None:
        assert isinstance(event, FundingAccrual)
        accruals.append(event)

    bus.subscribe(FundingAccrual, collect)
    net = dict(held or {})
    venue = _venue(bus, clock, account_net=lambda: net, funding_rate=funding_rate)
    if priced:
        await bus.publish(_tick("50000", ts=_at(start)))

    async with _supervising(venue):
        await _quiesce()
        clock.advance_to(_at(through))
        await _quiesce()
    return accruals


_LONG = {"BTC": Decimal("2")}


def test_a_jump_across_three_boundaries_accrues_three_separate_payments() -> None:
    """Funding is additive, so catch-up settles every boundary it crossed.

    A 2 BTC long at 50 000 at an hourly rate of 0.01% is 10 USDC per boundary.
    Advancing virtual time from 00:30 to 03:15 in one step crosses 01:00, 02:00
    and 03:00 — three distinct real payments. Collapsing them to one, as a
    convergent cadence would, silently under-charges by two hours of funding.
    """
    accruals = asyncio.run(_drive(held=_LONG, through=3.25))

    assert [accrual.boundary_ts_ns for accrual in accruals] == [_at(1), _at(2), _at(3)]
    assert [accrual.amount for accrual in accruals] == [Decimal("-10")] * 3


def test_a_position_this_process_never_filled_still_accrues() -> None:
    """The notional basis is what the **ledger** holds, not what this venue traded.

    Nothing is placed here and the venue fills nothing, yet the boundary is
    charged — because a paper venue is in-process and a position is not. Every
    life after the one that opened it inherits the position from the ledger and
    must go on charging it; a venue that answered from its own memory would find
    that memory empty and settle nothing, for this life and every one after,
    since only a new fill could ever refill it.

    Stated at the venue's own seam because that is where the basis is chosen.
    The two-life version — recover, then keep accruing — is
    ``tests/engine/test_funding_e2e.py``'s.
    """
    accruals = asyncio.run(_drive(held=_LONG, through=1.25))

    assert [accrual.amount for accrual in accruals] == [Decimal("-10")]


def test_the_held_size_is_re_read_every_span_rather_than_captured_at_start() -> None:
    """A position that moves between boundaries is priced at what is held *now*.

    The venue keeps no copy of the size to go stale, which is what makes the
    ledger its single owner rather than merely its seed. Halving the holding
    between two spans halves the next payment, with no restart and nothing to
    invalidate — and every path that moves the ledger without a paper fill
    (recovery, a heal, an absorbed unattributed fill) is covered by the same
    fact rather than each needing its own hook.
    """
    bus = InMemoryBus()
    clock = ManualClock(start_ns=_at(0.5))
    accruals: list[FundingAccrual] = []

    async def collect(event: Event) -> None:
        assert isinstance(event, FundingAccrual)
        accruals.append(event)

    async def scenario() -> None:
        bus.subscribe(FundingAccrual, collect)
        net = {"BTC": Decimal("2")}
        venue = _venue(bus, clock, account_net=lambda: net)
        await bus.publish(_tick("50000", ts=_at(0.5)))
        async with _supervising(venue):
            await _quiesce()
            clock.advance_to(_at(1.25))  # crosses 01:00 holding 2
            await _quiesce()
            net["BTC"] = Decimal("1")
            clock.advance_to(_at(2.25))  # crosses 02:00 holding 1
            await _quiesce()

    asyncio.run(scenario())

    assert [accrual.amount for accrual in accruals] == [Decimal("-10"), Decimal("-5")]


def test_neither_a_zero_rate_nor_a_flat_position_accrues_anything() -> None:
    """A zero amount emits **no event at all** — one rule covering both (ADR-0037).

    They are the same fact reached two ways: there is no payment to record, so
    there is nothing to key, nothing to make durable and no ledger churn on the
    default-``0`` path. Asserting them together is what pins it as one rule: a
    skip implemented only on the rate would leave a closed-out position paying
    funding on nothing, and one implemented only on the size would churn a keyed
    zero through the ledger for every frictionless spec.

    The flat case is a symbol the ledger reports at **zero** rather than one it
    omits: a symbol traded to flat keeps its partition (P1 #119), so the net is
    a real zero reaching the skip, not an absence sidestepping it.

    Both cases cross three boundaries, so neither is silent for want of time.
    """
    at_a_zero_rate = asyncio.run(_drive(held=_LONG, through=3.25, funding_rate="0"))
    flat_after_closing = asyncio.run(_drive(held={"BTC": Decimal("0")}, through=3.25))

    assert at_a_zero_rate == []
    assert flat_after_closing == []


def test_a_position_the_venue_cannot_price_settles_nothing_however_far_time_moved() -> None:
    """A venue with nothing to price accrues nothing, and skips the span whole.

    Paper has one price signal — the last trade — so a symbol it has never seen
    trade cannot be priced at all, and the alternative to skipping is inventing a
    price for a boundary. This is the restart gap one grain smaller and it became
    reachable only when the size moved to the ledger: a recovered position is
    held before this life has seen a single tick in its symbol.

    Load-bearing beyond the accrual it withholds. The span is skipped *before*
    its boundaries are enumerated, which is what keeps the first row of a replay
    cheap — the clock opens at zero and the first row jumps to a real timestamp,
    so an implementation that enumerated first and priced per boundary would walk
    every hour since the Unix epoch to produce exactly this nothing.

    Three boundaries cross, so the silence is not for want of time, and the same
    holding at the same rate pays over one boundary in the case above — so it is
    the missing price doing the work here and nothing else.
    """
    accruals = asyncio.run(_drive(held=_LONG, through=3.25, priced=False))

    assert accruals == []


def test_a_refused_ledger_write_raises_out_of_run_rather_than_dying_alone() -> None:
    """``run()``'s failure reaches whoever supervises it — the seam's whole point.

    The refusal reaches the loop because ``InMemoryBus.publish`` re-raises a
    handler's exception into its caller, and on this path the caller is the
    generator: a ledger write the store refuses therefore kills it, and this
    venue runs nothing else that would notice. What decides whether that matters
    is *who holds the task*. Held by the adapter, nobody is awaiting it and the
    engine runs on accruing nothing; held by the runner's ``TaskGroup`` — which
    is what awaiting ``run()`` here stands in for — the same exception aborts the
    group and faults the run at the refusal (ADR-0024, ADR-0043 §4).

    Asserted at the *moment* it happens rather than at a later teardown: the task
    is already done when the boundary's turn is over, so nothing had to be asked
    to stop for the failure to surface. The engine-level half — non-zero exit,
    ``FAULTED``, later boundaries never settled — is
    ``tests/engine/test_funding_e2e.py``'s.

    A cancellation is not a failure and stays invisible: every other case here
    ends the same loop by cancelling it and sees nothing, which is what pins this
    to a real exception rather than to the ending itself.
    """
    bus = InMemoryBus()
    clock = ManualClock(start_ns=_at(0.5))

    async def refuse(event: Event) -> None:
        raise InvariantViolation("ledger write refused")

    async def scenario() -> None:
        bus.subscribe(FundingAccrual, refuse)
        venue = _venue(bus, clock, account_net=lambda: dict(_LONG))
        await bus.publish(_tick("50000", ts=_at(0.5)))
        async with _supervising(venue) as running:
            await _quiesce()
            clock.advance_to(_at(1.25))
            await _quiesce()

            assert running.done()  # at the refusal, with nobody asked to stop
            with pytest.raises(InvariantViolation, match="ledger write refused"):
                await running

    asyncio.run(scenario())


def test_a_short_is_paid_over_the_same_boundary_a_long_would_pay() -> None:
    """The direction rides the held size, which is signed at the ledger's grain.

    Which side of a funding payment you are on *is* the direction of the
    position, so the sign travels with the size rather than being decided here.
    Holding −2 BTC is a short, and at the same positive rate the long paid 10
    USDC for, the short is credited 10.
    """
    accruals = asyncio.run(_drive(held={"BTC": Decimal("-2")}, through=1.25))

    assert [accrual.amount for accrual in accruals] == [Decimal("10")]
