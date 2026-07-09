"""``DrainLedger`` — the ``KafkaBus`` drain fence in isolation (ADR-0023/0024).

The subtlest, most off-by-one-prone code in the bus: how far this process has
*produced* per partition versus how far the poll loop has *committed*, and the
predicate that turns "our records are all committed" into "the topic went idle"
(single producer, single group — ADR-0001). Extracted to its own module so the
offset conventions get tested here directly, at their own interface, instead of
only end-to-end through a fake broker and a full saga.
"""

import asyncio

from tickwright.adapters.bus.drain_ledger import DrainLedger


def test_a_fresh_ledger_is_not_in_flight() -> None:
    # Nothing produced yet: there is nothing to wait for.
    assert DrainLedger().in_flight() is False


def test_a_produced_record_is_in_flight_until_it_is_committed() -> None:
    ledger = DrainLedger()
    ledger.record_produced(partition=0, offset=3)
    assert ledger.in_flight() is True

    ledger.record_committed(partition=0, offset=3)
    assert ledger.in_flight() is False


def test_committing_an_earlier_offset_leaves_the_high_water_in_flight() -> None:
    # `record_committed(offset)` means "consumed through `offset`", so the
    # topic is idle only once every partition's commit passes its high-water
    # mark — committing offset 2 does not discharge a record produced at 3.
    ledger = DrainLedger()
    ledger.record_produced(partition=0, offset=3)
    ledger.record_committed(partition=0, offset=2)
    assert ledger.in_flight() is True

    ledger.record_committed(partition=0, offset=3)
    assert ledger.in_flight() is False


def test_produced_high_water_is_a_max_not_the_latest_offset() -> None:
    # Records can be accounted out of order; the fence tracks the *highest*
    # offset produced, so a later, lower offset never lowers the bar.
    ledger = DrainLedger()
    ledger.record_produced(partition=0, offset=5)
    ledger.record_produced(partition=0, offset=2)

    ledger.record_committed(partition=0, offset=2)
    assert ledger.in_flight() is True  # still owe the record at offset 5

    ledger.record_committed(partition=0, offset=5)
    assert ledger.in_flight() is False


def test_the_topic_is_in_flight_until_every_partition_is_committed() -> None:
    ledger = DrainLedger()
    ledger.record_produced(partition=0, offset=0)
    ledger.record_produced(partition=1, offset=0)

    ledger.record_committed(partition=0, offset=0)
    assert ledger.in_flight() is True  # partition 1 still owes

    ledger.record_committed(partition=1, offset=0)
    assert ledger.in_flight() is False


def test_record_committed_wakes_a_waiter() -> None:
    async def scenario() -> None:
        ledger = DrainLedger()
        ledger.record_produced(partition=0, offset=0)
        waiter = asyncio.create_task(ledger.wait_for_progress())
        await asyncio.sleep(0)  # let the waiter reach .wait()
        assert not waiter.done()

        ledger.record_committed(partition=0, offset=0)
        await asyncio.wait_for(waiter, timeout=1)  # progress woke it

    asyncio.run(scenario())


def test_wait_for_progress_clears_a_stale_signal_before_waiting() -> None:
    # Correctness-critical: an already-set signal must not let wait_for_progress
    # return without yielding, or the drain loop would spin without ever handing
    # the event loop back to the poll task (starvation). Each wait blocks until
    # the *next* commit.
    async def scenario() -> None:
        ledger = DrainLedger()
        ledger.record_produced(partition=0, offset=1)
        ledger.record_committed(partition=0, offset=0)  # sets the signal (stale)

        waiter = asyncio.create_task(ledger.wait_for_progress())
        await asyncio.sleep(0)
        assert not waiter.done()  # the stale signal was cleared, not consumed

        ledger.record_committed(partition=0, offset=1)  # a fresh commit
        await asyncio.wait_for(waiter, timeout=1)

    asyncio.run(scenario())
