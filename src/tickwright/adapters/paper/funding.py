"""Paper's funding generator — the boundary loop the deterministic venue runs
(ADR-0037).

Live never runs this: the venue emits funding at its own boundaries and the
adapter ingests it. Paper has nobody to ask, so it **generates** the same keyed
``FundingAccrual`` shape off the injected ``Clock``.

Built on ``Clock.sleep_until``, the pure waiter (ADR-0033), and deliberately
**not** on ``run_cadence``. That loop reschedules from *now* with no catch-up,
which is right for the reconcile cadences because reconciling is **convergent** —
re-running heals nothing new, so replaying missed firings is noise. Funding is
the opposite: **additive**. Every boundary is a distinct real payment, so a
virtual-time jump across N of them must settle N, and collapsing would silently
under-charge. This is the one cadence in the engine that must catch up.

It lives in its own module rather than on ``PaperExchange`` so that a
non-terminating loop stays out of a class whose other 200 lines are a
synchronous matching book. Its life and its cancellation are the runner's:
``PaperExchange.run()`` awaits this, the runner supervises *that* in its
``TaskGroup``, and nothing here owns a task or knows how one ends.
"""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from tickwright.domain import (
    Clock,
    EventBus,
    FundingAccrual,
    InstrumentSpec,
    funding_amount,
    funding_boundaries,
)

HOUR_NS = 3_600_000_000_000
"""The venue's real funding interval and this generator's default (ADR-0037).

Epoch-aligned at one hour is exactly the top of each UTC hour — Hyperliquid's own
schedule. It stays a parameter rather than a constant so a future venue or a test
can settle on another interval, but it is deliberately **not** an environment
variable: nothing about a paper run's economics should be reachable from ambient
config that a live run could also read.
"""

_ZERO = Decimal("0")


@dataclass(frozen=True, slots=True, kw_only=True)
class FundingBasis:
    """What one symbol's accrual is computed from at a boundary.

    A snapshot, taken at the moment a boundary is settled: the **account net
    size** the ledger reports for the symbol (ADR-0034's Σ-invariant) and the
    price the venue would mark it at. The size is the ledger's because a paper
    venue is in-process and a position outlives processes; the price is the
    venue's because pricing is what a venue is for. ``price`` is paper's single
    signal — the last trade — collapsing the venue's oracle/mark distinction,
    which only bites where funding is computed against a real venue and so is
    moot here (ADR-0037).
    """

    symbol: str
    signed_size: Decimal
    price: Decimal
    spec: InstrumentSpec


async def run_funding(
    *,
    bus: EventBus,
    clock: Clock,
    account_id: str,
    basis: Callable[[], tuple[FundingBasis, ...]],
    interval_ns: int = HOUR_NS,
) -> None:
    """Settle every funding boundary crossed, for as long as the task lives.

    **Resumes from the startup instant, never from the durable watermark**
    (ADR-0043 §5.1). Boundaries that elapsed while the process was down are not
    settled: the catch-up pricing rule assumes a jump *between two consecutive
    ticks*, where position and last price are constant, and a restart gap is not
    that — the price genuinely moved and the venue's tick cache is empty at the
    moment catch-up would run. The accepted cost is that paper funding is **not
    restart-invariant** across a wall-clock gap; it understates by whatever
    elapsed while down, which is preferable to a restart-invariant number
    produced by a pricing rule nobody can defend.

    **The gap is the whole of the cost, and the boundaries after it are charged
    in full.** That is a claim about the notional basis rather than about this
    loop: the size comes from the ledger, which recovered the position, so the
    new life resumes charging it. It would not hold of a venue that tallied its
    own size — such a venue restarts empty, settles nothing, and only a fresh
    fill would ever wake it, so the "gap" would silently be every boundary from
    the restart onward.

    That is a statement about which boundaries this **produces**. Which of them
    are **applied** is the ledger's watermark to decide, and the two never meet on
    a wall-clock restart — which is why the gate is invisible there and
    load-bearing under replay, where the feed genuinely rewinds virtual time.

    ``basis`` is read **once per catch-up span**, not once per boundary, and
    ADR-0037 is what licenses that: a jump crosses boundaries *between* two
    ticks, where no fill happened and no price moved, so position and last price
    are constant across every boundary caught up. Catch-up is therefore "post N
    keyed payments distinguished only by their timestamps" rather than a
    re-simulation — and reading the basis per boundary would only invite the
    opposite reading.

    Once per span is nonetheless a genuine **re-read**, and the supplier is the
    ledger rather than any memory of this loop's or the venue's: nothing here is
    captured at start, so no path that moves the ledger can leave a stale size
    behind for the next boundary to price against.

    **A venue with nothing to price settles nothing, however far time moved.**
    That is the same argument as the restart gap one grain smaller: boundaries
    where nothing is held, or where nothing has traded yet to price it at, have
    no accrual to compute and no price to compute one at. It is also what keeps
    the first tick of a replay cheap — the clock opens at zero and the first row
    jumps to a real timestamp, so without it every hour since the Unix epoch
    would be enumerated to produce nothing.
    """
    settled_through_ns = clock.timestamp_ns()
    while True:
        await clock.sleep_until(settled_through_ns // interval_ns * interval_ns + interval_ns)
        now_ns = clock.timestamp_ns()
        exposure = basis()
        if exposure:
            for boundary_ns in funding_boundaries(
                after_ns=settled_through_ns, through_ns=now_ns, interval_ns=interval_ns
            ):
                await _settle(
                    bus=bus,
                    clock=clock,
                    account_id=account_id,
                    boundary_ns=boundary_ns,
                    basis=exposure,
                )
        settled_through_ns = now_ns


async def _settle(
    *,
    bus: EventBus,
    clock: Clock,
    account_id: str,
    boundary_ns: int,
    basis: tuple[FundingBasis, ...],
) -> None:
    """Publish one keyed accrual per symbol that owes or is owed one.

    **A zero accrues nothing at all** — no event, no ledger churn — which covers
    both a flat position and a ``funding_rate`` of ``0``, the default. One rule
    rather than two, because it is one fact: there is no payment to record. It is
    safe to leave no durable trace of a zero precisely because catch-up
    re-derives boundaries and re-skips them (ADR-0037).
    """
    for entry in basis:
        amount = funding_amount(signed_size=entry.signed_size, price=entry.price, spec=entry.spec)
        if amount == _ZERO:
            continue
        await bus.publish(
            FundingAccrual(
                # The boundary is when the fact occurred; ``ts_init`` is when the
                # object was built, which on a caught-up boundary is genuinely
                # later (ADR-0005).
                ts_event=boundary_ns,
                ts_init=clock.timestamp_ns(),
                account_id=account_id,
                symbol=entry.symbol,
                boundary_ts_ns=boundary_ns,
                amount=amount,
            )
        )
