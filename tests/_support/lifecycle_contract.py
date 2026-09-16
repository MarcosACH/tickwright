"""The clause every supervised long-lived half owes, on either seam.

``MarketFeed`` and ``Exchange`` both declare ``run()``, and the runner ends both
the same way: ``stop()`` as a request, then cancel the task, then wait it out
(``Engine._stop_supervised``, #277). The wait is what proves the loop ended
before ``bus.drain`` starts. A ``run()`` that caught ``CancelledError`` and
published once more would satisfy either Protocol, type-check, and pass a suite
of its own, while keeping the drain's high-water mark rising.

One module for both seams, because the clause is about the lifecycle and not
about what the loop publishes. A feed publishes ticks and marks, a paper venue
publishes funding accruals, and a third adapter may publish something else.
So the transcript here counts every ``Event`` and reads none of them.

**Shared assertion, per-adapter driving**, as in ``feed_contract.py``: the four
shipped adapters share no lifecycle worth parametrizing, so each suite drives
its adapter in its own idiom and hands the transcript, the adapter and the
task here.

Explicit assertion messages throughout: this module is not a test module, so
pytest does not rewrite its asserts and a bare comparison would fail blind.
"""

import asyncio
from dataclasses import dataclass, field

from tickwright.domain import Event, EventBus, Exchange, MarketFeed


@dataclass
class PublishTranscript:
    """Every event one adapter run put on the bus, and which of them came late.

    ``late`` holds the events that reached the bus after the watched task was
    done. It is a second list and not a count taken after the wait, because a
    publish the loop handed to a task of its own lands on one of the turns the
    wait itself spends, where a count taken afterwards already includes it.
    """

    events: list[Event] = field(default_factory=list)
    late: list[Event] = field(default_factory=list)
    watched: asyncio.Task[None] | None = None


def record_publishes(bus: EventBus) -> PublishTranscript:
    """Subscribe to every event type and return the transcript they fill.

    Subscribe before driving the loop, and after anything the driver itself
    puts on the bus. The clause asks whether the loop went quiet, so the
    transcript must hold the loop's publishes and not the driver's.
    """
    transcript = PublishTranscript()

    async def record(event: Event) -> None:
        transcript.events.append(event)
        if transcript.watched is not None and transcript.watched.done():
            transcript.late.append(event)

    bus.subscribe(Event, record)
    return transcript


async def assert_quiet_once_stopped(
    transcript: PublishTranscript,
    *,
    adapter: MarketFeed | Exchange,
    task: asyncio.Task[None],
    name: str,
) -> None:
    """End ``adapter`` the way the runner does, then assert nothing more reaches the bus.

    The runner's ``_stop_supervised`` shape, stated once as an obligation
    (#277): ``stop()`` is a request, the cancel is what ends ``run()``, and the
    wait is what proves it ended. Two things are owed. ``run()`` ends under the
    cancel, promptly. Whether it had already returned on the ``stop()`` alone
    is the adapter's own, so nothing here asks which of the two ended it. And
    once the task is done, nothing more reaches the bus, so no publish of the
    loop's outlives its supervised half and reaches the ``bus.drain`` behind it.

    The bound on the wait is what makes the first half sensitive. A ``run()``
    that caught ``CancelledError`` and kept looping would otherwise hang this
    helper, and a hang reports nothing. The turns yielded after the wait are
    what make the second half sensitive. A publish the loop handed to a task of
    its own lands on one of them, and the transcript marks it late because the
    watched task was already done. A publish awaited inside ``run()`` itself,
    even from its ``finally``, is not late: the task is not done yet, and the
    runner's wait covers it.

    The driver ends the adapter with at least one event already on the bus, and
    that is refused here for the reason the mark clause refuses an empty
    transcript: a loop that never published proves the clause by proving
    nothing.
    """
    assert transcript.events, (
        f"{name} published nothing before it was stopped, so this run says nothing "
        f"about quiescence — drive the loop until something reaches the bus first"
    )
    transcript.watched = task
    await adapter.stop()
    task.cancel()
    try:
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=2)
    except TimeoutError:
        raise AssertionError(
            f"{name}.run() did not end once cancelled — CancelledError is the ordinary "
            f"end of run() and must come through, or the runner's wait for it never "
            f"returns and the graceful stop faults on its bound"
        ) from None
    for _ in range(10):
        await asyncio.sleep(0)
    assert not transcript.late, (
        f"{name} published {len(transcript.late)} event(s) after its run() ended — "
        f"anything still publishing keeps raising bus.drain's high-water mark (ADR-0024)"
    )
