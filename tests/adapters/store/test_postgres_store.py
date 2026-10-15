"""Postgres-specific ``Store`` behavior (ADR-0019).

The rules both stores share live in ``test_store_contract.py``. What is here
only ``PostgresStore`` has. Its lock is held by a server session (ADR-0052).
So the holder is a session with a pid and a client address. A lost session is
a lost lock. And the session carries short keepalives.

Every test needs a real server. The ``postgres`` marker is on the whole module,
and each test skips when no ``STORE_POSTGRES_DSN`` is reachable. So
``uv run pytest`` stays hermetic.
"""

from decimal import Decimal
from pathlib import Path

import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from store_backends import PostgresBackend, resolve_backend

from tickwright.adapters.store.postgres import PostgresStore
from tickwright.domain import InvariantViolation, Order, OrderType, Side

pytestmark = pytest.mark.postgres


@pytest.fixture
def backend(tmp_path: Path) -> PostgresBackend:
    """A Postgres backend with every table empty, or a skip when none is reachable."""
    resolved = resolve_backend("postgres", tmp_path / "unused.db")
    assert isinstance(resolved, PostgresBackend)
    return resolved


def _order() -> Order:
    return Order(
        cloid="0xabc",
        strategy_id="trivial",
        signal_id="trivial:BTC:1",
        symbol="BTC",
        side=Side.BUY,
        quantity=Decimal("2"),
        order_type=OrderType.MARKET,
    )


def test_a_postgres_holder_pid_is_the_session_that_holds_the_lock(
    backend: PostgresBackend,
) -> None:
    """The refusal tells the operator to run ``pg_terminate_backend(<pid>)``
    (ADR-0052). That only works if the pid is the holder's own session, so ending
    that session must free the lock."""
    with backend.open() as first, backend.open() as second:
        assert first.lock() is None
        holder = second.lock()
        assert holder is not None
        assert holder.pid is not None

        backend.terminate(holder.pid)

        assert second.lock() is None


def test_a_postgres_holder_names_the_store_without_its_credentials(
    backend: PostgresBackend,
) -> None:
    """``exit.refused`` names the store, and that event goes to the operator's
    logs (ADR-0060). So it carries the host and database, never the password."""
    with backend.open() as first, backend.open() as second:
        assert first.lock() is None
        holder = second.lock()
        assert holder is not None

        dsn = conninfo_to_dict(backend.dsn)
        assert holder.store == f"{dsn['host']}:{dsn.get('port', 5432)}/{dsn['dbname']}"


def test_a_postgres_store_names_itself_without_its_credentials(
    backend: PostgresBackend,
) -> None:
    """``exit.refused`` and ``exit.new_store`` name the store in the operator's
    logs (ADR-0060). So the name carries the host and database, never the password."""
    with backend.open() as store:
        dsn = conninfo_to_dict(backend.dsn)
        assert store.name == f"{dsn['host']}:{dsn.get('port', 5432)}/{dsn['dbname']}"


def test_a_postgres_refusal_says_where_the_holder_runs_and_when_to_retry(
    backend: PostgresBackend,
) -> None:
    """The operator must tell a dead engine from a live one before ending its
    session. So the refusal names the holder's client address and start time.
    A dead holder frees the lock in about 25 seconds, so it says to retry in
    about 30 (ADR-0052)."""
    with backend.open() as first, backend.open() as second:
        assert first.lock() is None
        holder = second.lock()
        assert holder is not None and holder.pid is not None

        client, started = backend.session(holder.pid)
        assert client in holder.detail
        assert started in holder.detail
        assert "retry in about 30 seconds" in holder.detail


def test_a_postgres_store_that_lost_its_session_refuses_its_next_write(
    backend: PostgresBackend,
) -> None:
    """A lost session is a lost lock, and another engine may now hold it. The
    store never reconnects, so its next write fails, and ``InvariantViolation``
    faults the engine before it can place another order (ADR-0052)."""
    with backend.open() as lost:
        assert lost.lock() is None
        with backend.open() as probe:
            holder = probe.lock()
        assert holder is not None and holder.pid is not None

        backend.terminate(holder.pid)

        with pytest.raises(InvariantViolation):
            lost.checkpoint(_order(), ts_ns=1_000)
        with backend.open() as successor:
            assert successor.lock() is None


def test_a_postgres_store_session_carries_the_adr_0052_keepalives(
    backend: PostgresBackend,
) -> None:
    """Without keepalives, the server keeps a dead engine's session, and its
    lock, for about two hours. ADR-0052 sets these four values so it takes about
    25 seconds. Only the session itself can show its settings, so this reads
    them through the store's own connection."""
    with backend.open() as store:
        rows = store._conn.execute(
            "SELECT name, setting FROM pg_settings WHERE name IN "
            "('tcp_keepalives_idle', 'tcp_keepalives_interval', "
            "'tcp_keepalives_count', 'tcp_user_timeout')"
        ).fetchall()

    assert dict(rows) == {
        "tcp_keepalives_idle": "10",
        "tcp_keepalives_interval": "5",
        "tcp_keepalives_count": "3",
        "tcp_user_timeout": "25000",
    }


def test_a_postgres_store_connection_times_out_on_its_own_side_even_over_the_dsn(
    backend: PostgresBackend,
) -> None:
    """Without client-side keepalives, a store call on a dropped network can
    hang the engine for about 15 minutes. ADR-0052 bounds it to about 25
    seconds. The engine's values win over a DSN's own, so an operator cannot
    turn the bound off by accident."""
    dsn = make_conninfo(backend.dsn, keepalives_idle=600)
    expected = {
        "keepalives": "1",
        "keepalives_idle": "10",
        "keepalives_interval": "5",
        "keepalives_count": "3",
        "tcp_user_timeout": "25000",
    }

    with PostgresStore(dsn) as store:
        params = store._conn.info.get_parameters()

    assert {name: params.get(name) for name in expected} == expected
