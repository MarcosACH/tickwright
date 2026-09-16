"""The behavioural contract every ``MarketFeed`` owes, whatever venue it reads.

``MarketFeed``'s docstring used to be one line about ``MarketTick``, and since
[#175](https://github.com/MarcosACH/tickwright/issues/175) both adapters also
emit a ``MarkTick`` — the obligation recorded in ``docs/extending.md``'s
checklist and nowhere executable. A third venue feed that omitted the mark would
satisfy the Protocol, type-check, run, and pass a suite of its own, while
``PortfolioProjection._marks`` stayed empty and every Tier-2 figure downstream
read ``None`` forever with nothing red (ADR-0034/0039).

``seam_claims.py`` is the wrong instrument for it, and says so itself: that gate
is **member-grained**, so it sees that ``start`` carries a claim and never that
the claim covers every clause ``start``'s docstring makes. The mark *is* such a
clause. This is the clause-grained half, and it can only be behavioural.

**Shared assertion, per-adapter driving** — which is how the ticket's stated
tension resolves. ``Store``'s contract drives every member from one map because
its backends promise identical behaviour; ``MarketFeed``'s two do not, and not
in a way any parametrization papers over: ``ReplayFeed`` reads a finite file and
returns, ``HyperliquidFeed`` opens a socket and never does. So the *driving*
stays with the adapter that knows how, in its own suite and its own idiom
(``Exchange``'s precedent), and only the obligation lives here — assumed of no
frame format, no lifecycle and no clock. What crosses the boundary is a
transcript of what reached the bus.

**Provenance stays out.** ADR-0039 puts one provenance per deployment — replay
derives the last-trade proxy, live reads ``ctx.markPx`` — so *how* a mark was
arrived at is each adapter's own assertion and differs by design. Only the
obligation to publish one is shared.

Explicit assertion messages throughout: this module is not a test module, so
pytest does not rewrite its asserts and a bare comparison would fail blind.
"""

import asyncio
from dataclasses import dataclass, field

from tickwright.domain import EventBus, MarketFeed, MarketTick, MarkTick


@dataclass(frozen=True)
class MarketDataTranscript:
    """What one feed run put on the bus, per market-data stream.

    Both streams in one record rather than two lists a caller pairs up: the
    obligation is a statement *relating* them, so handing them over separately
    would let a suite drive the contract with a tick list and a mark list from
    two different runs.
    """

    ticks: list[MarketTick] = field(default_factory=list)
    marks: list[MarkTick] = field(default_factory=list)


def record_market_data(bus: EventBus) -> MarketDataTranscript:
    """Subscribe both market-data streams and return the transcript they fill.

    Returned before the feed runs, and filled as it does: the caller subscribes
    here, drives its adapter however that adapter is driven, then asserts. The
    bus is the observation point precisely because it is the one the engine
    uses — a contract that reached into the adapter for its marks would pass a
    feed that computed them and never published.
    """
    transcript = MarketDataTranscript()

    async def record_tick(tick: MarketTick) -> None:
        transcript.ticks.append(tick)

    async def record_mark(mark: MarkTick) -> None:
        transcript.marks.append(mark)

    bus.subscribe(MarketTick, record_tick)
    bus.subscribe(MarkTick, record_mark)
    return transcript


def assert_every_traded_symbol_is_marked(transcript: MarketDataTranscript, *, feed: str) -> None:
    """Assert every symbol ``feed`` traded also reached the bus as a ``MarkTick``.

    Symbol-grained, not tick-grained, and deliberately not a count: the two
    streams are independent on a live venue — ``activeAssetCtx`` updates every
    few seconds while ``trades`` fires per trade — so anything pairing them
    one-for-one would assert a cadence no venue promises. What must hold is that
    a symbol the engine can hold a position in is a symbol it can value.

    Every unmarked symbol at once, sorted: an operator adding a venue wants the
    whole omission, not the alphabetically first one per run.

    An empty transcript is refused before any of that. "Every traded symbol is
    marked" is trivially true of a run that traded nothing, so a driver wired up
    wrong — subscribed after the feed ran, stopped before the first frame,
    pointed at an empty fixture — would prove the obligation by proving nothing,
    and would do it green. No such guard on the marks: a live venue really does
    publish ``activeAssetCtx`` for a symbol that has not traded, so a mark-only
    transcript is a fact about the venue, while a trade-less one is only ever a
    fact about the driver.
    """
    assert transcript.ticks, (
        f"{feed} published no trades at all, so this run says nothing about the mark "
        f"obligation — drive the feed until at least one MarketTick reaches the bus, "
        f"and subscribe (record_market_data) before driving it, not after"
    )
    traded = {tick.symbol for tick in transcript.ticks}
    marked = {mark.symbol for mark in transcript.marks}
    unmarked = traded - marked
    assert not unmarked, (
        f"{feed} published trades for {sorted(unmarked)} and never a mark for them — "
        f"a MarketFeed owes a MarkTick per traded symbol (ADR-0039), or every "
        f"unrealized_pnl, notional and equity for those symbols reads None forever"
    )


async def assert_quiet_once_stopped(
    transcript: MarketDataTranscript, *, feed: MarketFeed, task: asyncio.Task[None], name: str
) -> None:
    """End ``feed`` the way the runner does, then assert nothing more reaches the bus.

    The runner's ``_stop_supervised`` shape, stated once as an obligation
    (#277): ``stop()`` is a request, the cancel is what ends ``run()``, and the
    wait is what proves it ended. Two things are owed. ``run()`` ends under the
    cancel, promptly. Whether it had already returned on the ``stop()`` alone
    is the adapter's own, so nothing here asks which of the two ended it. And
    once the task is done, the transcript stops growing, so no publish of the
    feed's outlives its supervised half and reaches the ``bus.drain`` behind it.

    The bound on the wait is what makes the first half sensitive. A ``run()``
    that caught ``CancelledError`` and kept looping would otherwise hang this
    helper, and a hang reports nothing. The turns yielded after the wait are
    what make the second half sensitive. A publish the feed handed to a task of
    its own would land on one of them, where a check made on the same turn as
    the wait would miss it.

    The driver ends the feed with at least one event already on the bus, and
    that is refused here for the reason the mark clause refuses an empty
    transcript: a loop that never published proves the clause by proving
    nothing.
    """
    assert transcript.ticks or transcript.marks, (
        f"{name} published nothing before it was stopped, so this run says nothing "
        f"about quiescence — drive the feed until something reaches the bus first"
    )
    await feed.stop()
    task.cancel()
    try:
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=2)
    except TimeoutError:
        raise AssertionError(
            f"{name}.run() did not end once cancelled — CancelledError is the ordinary "
            f"end of run() and must come through, or the runner's wait for it never "
            f"returns and the graceful stop faults on its bound"
        ) from None
    ticks, marks = len(transcript.ticks), len(transcript.marks)
    for _ in range(10):
        await asyncio.sleep(0)
    assert (len(transcript.ticks), len(transcript.marks)) == (ticks, marks), (
        f"{name} published after its run() was cancelled and waited out — anything "
        f"still publishing keeps raising bus.drain's high-water mark (ADR-0024)"
    )
