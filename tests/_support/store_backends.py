"""One store per backend, for the contract suite shared across both (ADR-0019).

The parity promise is that swapping the store changes durability, never
behavior, so the store contract suite takes a ``backend`` parameter and drives
the same scenario through whichever one it names. ``SQLiteStore`` is the
hermetic real thing (a temp file, so a reopen reads it back); ``PostgresStore``
runs against a real server addressed by ``STORE_POSTGRES_DSN`` and is
auto-skipped when none is reachable, so the default suite stays zero-setup.

A backend exposes ``open()`` — a fresh store over the *same* durable backing —
so a test can checkpoint, close, reopen, and prove the record survived.
"""

import os
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from tickwright.adapters.store import SQLiteStore
from tickwright.adapters.store.postgres import PostgresStore

POSTGRES_DSN_ENV = "STORE_POSTGRES_DSN"

# Parametrization for the contract suite: the Postgres arm carries the
# ``postgres`` marker so ``-m "not postgres"`` deselects it wholesale, on top of
# the fixture's DSN-availability auto-skip.
STORE_BACKEND_PARAMS = [
    "sqlite",
    pytest.param("postgres", marks=pytest.mark.postgres),
]


class SQLiteBackend:
    """A file-backed SQLite store at a fixed path — a reopen reads the same file."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def open(self) -> SQLiteStore:
        return SQLiteStore(self._path)


class PostgresBackend:
    """A Postgres store at a fixed DSN — a reopen reconnects to the same server."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def open(self) -> PostgresStore:
        return PostgresStore(self._dsn)

    def reset(self) -> None:
        """Create the schema (a store open runs the DDL) and truncate every table,
        so each test starts from a clean, isolated slate against the shared server.

        The table list comes from the server's own catalog rather than a literal
        here. A transcribed list goes stale silently and in the one direction
        nothing catches: a table omitted from it leaks rows between Postgres
        tests without failing anything, and the always-on SQLite arm cannot
        notice, because ``conftest`` hands that arm a fresh ``tmp_path`` per test.
        So the isolation of the arm that only runs under ``-m postgres`` would
        rest on someone remembering to edit this string.

        **The fixture therefore owns the whole schema, not three named tables**,
        and ``STORE_POSTGRES_DSN`` must address a database dedicated to this
        suite — the ``docker compose up -d postgres`` service is exactly that.
        Pointed at a database holding anything else, this truncates it. That is
        the price of asking the server instead of transcribing the answer, and
        it is the cheaper side: a too-narrow list corrupts test results silently,
        while a too-wide one can only destroy data the suite was already told to
        treat as disposable.
        """
        PostgresStore(self._dsn).close()
        with psycopg.connect(self._dsn, autocommit=True) as conn:
            tables = [
                name
                for (name,) in conn.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
                ).fetchall()
            ]
            if tables:
                conn.execute(
                    sql.SQL("TRUNCATE {}").format(
                        sql.SQL(", ").join(sql.Identifier(name) for name in tables)
                    )
                )


def resolve_backend(name: str, sqlite_path: Path) -> SQLiteBackend | PostgresBackend:
    """The backend named ``name``, or a ``pytest.skip`` when Postgres is unavailable.

    ``sqlite`` is always available. ``postgres`` needs a reachable server: with no
    ``STORE_POSTGRES_DSN`` the arm skips (default zero-setup path); with one set
    but unreachable it skips with the connection error, never a hard failure.
    """
    match name:
        case "sqlite":
            return SQLiteBackend(sqlite_path)
        case "postgres":
            dsn = os.environ.get(POSTGRES_DSN_ENV)
            if not dsn:
                pytest.skip(f"no {POSTGRES_DSN_ENV}: Postgres store contract skipped")
            backend = PostgresBackend(dsn)
            try:
                backend.reset()
            except psycopg.OperationalError as exc:
                pytest.skip(f"{POSTGRES_DSN_ENV} set but Postgres unreachable: {exc}")
            return backend
        case unknown:
            raise ValueError(f"unknown store backend {unknown!r}")
