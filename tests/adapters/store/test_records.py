"""The shared row shape both ``Store`` adapters write through (ADR-0019).

The contract suite proves the two backends *behave* identically over a real
server each. These cases prove the narrower thing that makes that cheap: the SQL
they behave identically through is one statement rendered twice, so a change to
what a row is cannot reach one backend and miss the other.

The claim under test is the module docstring's — that the parameter marker is
the whole of the per-dialect difference. Asserted on the rendered text, because
that is the level the two backends could silently diverge at.
"""

from tickwright.adapters.store._records import (
    ACCOUNT_COLUMNS,
    ACCOUNT_UPDATE_COLUMNS,
    POSITION_COLUMNS,
    RECORD_COLUMNS,
    Upserts,
    upserts_for,
)

_STATEMENTS = tuple(field for field in Upserts.__dataclass_fields__)


def test_the_two_dialects_differ_only_in_the_parameter_marker() -> None:
    """``?`` against ``%s`` and nothing else. Every other token — table, column
    order, conflict target, update list — is one decision rendered twice, which
    is what lets the backends be held to one behavior by one contract suite."""
    sqlite = upserts_for("?")
    postgres = upserts_for("%s")

    for statement in _STATEMENTS:
        assert getattr(sqlite, statement) == getattr(postgres, statement).replace("%s", "?")


def test_every_write_carries_one_placeholder_per_column() -> None:
    """The statement and the write tuple are rendered from one column list, so a
    column added to the list cannot leave the statement short a marker — the
    drift the shared builder exists to make unrepresentable."""
    upserts = upserts_for("?")

    assert upserts.order.count("?") == len(RECORD_COLUMNS)
    assert upserts.position.count("?") == len(POSITION_COLUMNS)
    # ``account`` carries one more column than markers: ``id`` is written as the
    # literal ``1``, being the ``CHECK (id = 1)`` constraint rather than a value
    # any caller supplies (ADR-0043 §3).
    assert upserts.account.count("?") == len(ACCOUNT_COLUMNS)
    assert "VALUES (1, " in upserts.account


def test_the_accounts_write_once_trio_is_absent_from_its_update_list() -> None:
    """ "Written once, with the row; never updated" (ADR-0043 §3) is a property of
    the *statement*, not of the caller's discipline: a second checkpoint carrying
    a different genesis cannot move it, because no upsert names it."""
    account = upserts_for("?")
    _, update_clause = account.account.split("DO UPDATE SET ")

    for column in ("account_id", "genesis_collateral", "genesis_ts_ns"):
        assert column not in update_clause
    for column in ACCOUNT_UPDATE_COLUMNS:
        assert f"{column} = EXCLUDED.{column}" in update_clause
