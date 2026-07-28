"""The shared row shape both ``Store`` adapters write through (ADR-0019).

The contract suite proves the two backends *behave* identically over a real
server each. These cases prove the narrower things that make that cheap and that
no backend round trip can reach: that the SQL they behave identically through is
one statement rendered twice, and that the ``NULL`` a column admits is a value
this mapping can both write and read — a state the schema allows but no
round-trip test can observe, because the two directions cancel.

Asserted on the rendered text and on the write tuple, because those are the
levels at which the two backends could silently diverge.
"""

from decimal import Decimal

from tickwright.adapters.store._records import (
    ACCOUNT_COLUMNS,
    ACCOUNT_UPDATE_COLUMNS,
    POSITION_COLUMNS,
    RECORD_COLUMNS,
    Upserts,
    position_values,
    restore_position,
    upserts_for,
)
from tickwright.domain import Position

_STATEMENTS = tuple(field for field in Upserts.__dataclass_fields__)

_ENTRY_PRICE = POSITION_COLUMNS.index("entry_price")


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


def test_a_flat_position_writes_a_null_entry_price() -> None:
    """``NULL`` says "no position to have an entry for"; ``0`` would be
    indistinguishable from a real price (ADR-0043 §3), which is why the column is
    nullable at all. A full close resets entry (P1 #119), so ``is_flat`` is
    exactly the condition — the aggregate never holds an entry worth keeping on a
    row with no exposure."""
    flat = Position(strategy_id="trivial", symbol="BTC", realized_pnl=Decimal("125.5"))

    assert flat.is_flat
    assert position_values(flat, ts_ns=1_000)[_ENTRY_PRICE] is None


def test_an_open_position_writes_its_entry_price() -> None:
    """The other half of the rule: a row with exposure carries a real price, and
    the nullability must not swallow it."""
    open_position = Position(
        strategy_id="trivial",
        symbol="BTC",
        signed_size=Decimal("2"),
        entry_price=Decimal("50000.25"),
    )

    assert position_values(open_position, ts_ns=1_000)[_ENTRY_PRICE] == "50000.25"


def test_a_null_entry_price_restores_a_flat_position() -> None:
    """The read half. ``Decimal(None)`` raises ``TypeError``, so without this the
    mapping could write a row neither backend could read back — and the schema
    admits the row whether or not the mapping does.

    It restores to ``0`` rather than ``None`` because the aggregate has no
    ``None`` to hold: ADR-0041 §3 has a flat-with-history record read
    ``entry_price=0`` through the seam. The distinction the column keeps is on
    disk, where a reader can tell "never had an entry" from a real price."""
    row = ("trivial", "BTC", "0", None, "125.5", "2", "-0.5", "0", 1_000)

    restored = restore_position(row)

    assert restored.is_flat
    assert restored.entry_price == Decimal("0")
    assert restored.realized_pnl == Decimal("125.5")


def test_a_null_isolated_collateral_restores_a_cross_margined_position() -> None:
    """The same read half for the other nullable column (ADR-0043 §3), where
    ``NULL`` means *cross-margined* rather than an isolated position wiped to
    ``0``. No writer produces it yet — margin mode does not exist in the model —
    but the schema admits the row today, and ``Decimal(None)`` raises
    ``TypeError``, which is not a driver error and so would escape
    ``all_positions()`` unwrapped, past the seam's ``InvariantViolation``
    contract, on the recovery path.

    It restores to ``0`` for the reason ``entry_price`` does: the aggregate has
    no ``None`` to hold, and the distinction the column keeps is the durable one.
    The *write* branch — emitting ``NULL`` for a genuinely cross-margined
    position — belongs to the slice that first produces one (#190/#176)."""
    row = ("trivial", "BTC", "2", "50000.25", "0", "0", "0", None, 1_000)

    restored = restore_position(row)

    assert restored.isolated_collateral == Decimal("0")
    assert restored.entry_price == Decimal("50000.25")
