"""``SQLiteStore`` config — each adapter's config lives in its package
(ADR-0032); only the composition root reads them all."""

from pathlib import Path

from pydantic import BaseModel


class SQLiteStoreConfig(BaseModel):
    """Where the durable saga checkpoints live. ``:memory:`` is for tests —
    a crash-safety story needs a file that outlives the process."""

    path: Path = Path("tickwright.db")
