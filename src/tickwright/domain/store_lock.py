"""Who holds a store's lock when another process asks for it (ADR-0052)."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True, kw_only=True)
class StoreLockHolder:
    """The process that holds a store's lock.

    ``pid`` is the process to end: an OS pid on SQLite, a server session pid on
    Postgres. It is ``None`` when the holder has not written it yet. ``detail``
    is the adapter's own sentence for the operator. It names the holder and says
    how to end it, because only the adapter knows how.
    """

    pid: int | None
    detail: str
