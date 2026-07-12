"""Fixtures for the store contract suite (ADR-0019).

``store_backend`` is parametrized over both ``Store`` adapters: the SQLite arm
runs always, the Postgres arm auto-skips unless a server is reachable.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from store_backends import (
    STORE_BACKEND_PARAMS,
    PostgresBackend,
    SQLiteBackend,
    resolve_backend,
)


@pytest.fixture(params=STORE_BACKEND_PARAMS)
def store_backend(
    request: pytest.FixtureRequest, tmp_path: Path
) -> Iterator[SQLiteBackend | PostgresBackend]:
    """A backend factory for the contract suite: ``.open()`` yields a fresh store
    over the same durable backing, so a test can close and reopen it."""
    yield resolve_backend(request.param, tmp_path / "saga.db")
