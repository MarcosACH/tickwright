"""Store adapter configs — each adapter's config lives in its package
(ADR-0032); only the composition root reads them all. Both are plain pydantic
(no driver import), so importing them never loads sqlite3 or psycopg."""

from pathlib import Path

from pydantic import BaseModel


class SQLiteStoreConfig(BaseModel):
    """Where the durable saga checkpoints live. ``:memory:`` is for tests —
    a crash-safety story needs a file that outlives the process."""

    path: Path = Path("tickwright.db")


class PostgresStoreConfig(BaseModel):
    """The libpq DSN for the production-parity store (ADR-0019), read only when
    ``store=postgres``. The default targets the local ``docker compose`` service;
    real deployments point it at their managed Postgres."""

    dsn: str = "postgresql://tickwright:tickwright@localhost:5432/tickwright"
