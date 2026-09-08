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

from dataclasses import dataclass, field

from tickwright.domain import EventBus, MarketTick, MarkTick


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
    """
    traded = {tick.symbol for tick in transcript.ticks}
    marked = {mark.symbol for mark in transcript.marks}
    unmarked = traded - marked
    assert not unmarked, (
        f"{feed} published trades for {sorted(unmarked)} and never a mark for them — "
        f"a MarketFeed owes a MarkTick per traded symbol (ADR-0039), or every "
        f"unrealized_pnl, notional and equity for those symbols reads None forever"
    )
