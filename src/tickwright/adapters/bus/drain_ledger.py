"""``DrainLedger`` — the ``KafkaBus`` drain fence (ADR-0023/0024).

Produced-versus-committed offset accounting, per partition: the highest offset
this process has produced against the position the poll loop has committed.
Single producer, single group (ADR-0001) — we are the only producer, so "every
record we produced is committed" is exactly "the topic went idle", the Kafka
face of the in-memory drain-to-quiescence contract. ``KafkaBus`` records a
produce on ``publish``, records a commit in its poll loop, and waits on this
ledger in ``drain`` until nothing is in flight.

Offset convention (aiokafka's): ``record_produced``/``record_committed`` both
take the offset a record *landed at*; a commit means "consumed through this
offset", so the partition is idle once its committed position has passed its
produced high-water mark.
"""

import asyncio


class DrainLedger:
    """Per-partition produced/committed offsets, and the wait that fences a drain."""

    def __init__(self) -> None:
        self._produced_high_water: dict[int, int] = {}
        self._committed_position: dict[int, int] = {}
        self._progress = asyncio.Event()

    def record_produced(self, partition: int, offset: int) -> None:
        """Note that this process produced a record at ``offset`` on ``partition``."""
        current = self._produced_high_water.get(partition, -1)
        self._produced_high_water[partition] = max(current, offset)

    def record_committed(self, partition: int, offset: int) -> None:
        """Note the poll loop consumed and committed through ``offset``, and signal progress."""
        self._committed_position[partition] = offset + 1
        self._progress.set()

    def in_flight(self) -> bool:
        """Whether any partition's committed position has not yet passed its high-water mark."""
        return any(
            self._committed_position.get(partition, 0) <= high_water
            for partition, high_water in self._produced_high_water.items()
        )

    async def wait_for_progress(self) -> None:
        """Block until the next ``record_committed``.

        Clears the signal *before* waiting so a stale one from an earlier commit
        cannot let this return without yielding — that would spin the drain loop
        and starve the poll task. Each call blocks until fresh progress arrives.
        """
        self._progress.clear()
        await self._progress.wait()
