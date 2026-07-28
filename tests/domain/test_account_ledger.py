"""The ``Account`` aggregate: one cash line, a closed write-set, a write-once
opening declaration (ADR-0042 §3/§4)."""

from decimal import Decimal

import pytest

from tickwright.domain import Account, AccountSpec, Netting


def _account(genesis: str = "100000") -> Account:
    return Account(account_id="paper-default", genesis_collateral=Decimal(genesis), genesis_ts_ns=7)


def test_an_account_opens_with_its_cash_line_at_the_genesis_collateral() -> None:
    account = _account("250000")

    assert account.account_id == "paper-default"
    assert account.cash == Decimal("250000")
    assert account.genesis_collateral == Decimal("250000")
    assert account.genesis_ts_ns == 7
    assert account.view().cash == Decimal("250000")


def test_realized_pnl_accrues_to_the_cash_line() -> None:
    account = _account("1000")

    assert account.accrue_realized(Decimal("-40"), event_id="0xabc:fill:f1") is True

    assert account.cash == Decimal("960")


def test_a_redelivered_accrual_moves_no_cash() -> None:
    account = _account("1000")
    account.accrue_realized(Decimal("250"), event_id="0xabc:fill:f1")

    assert account.accrue_realized(Decimal("250"), event_id="0xabc:fill:f1") is False

    assert account.cash == Decimal("1250")
    # The dedup set is contract, not bookkeeping: the durable ledger round-trips
    # it so a fill that predates a restart is still a no-op after one.
    assert account.applied_event_ids == frozenset({"0xabc:fill:f1"})


def test_the_opening_declaration_is_write_once() -> None:
    """Genesis is the account's creation seed; the cash line accumulates away
    from it and never moves it back (ADR-0042 §3)."""
    account = _account("1000")
    account.accrue_realized(Decimal("500"), event_id="0xabc:fill:f1")

    assert account.genesis_collateral == Decimal("1000")
    with pytest.raises(AttributeError):
        account.genesis_collateral = Decimal("9")  # type: ignore[misc]


def test_an_account_spec_declares_the_venue_facts_and_defaults_to_net() -> None:
    spec = AccountSpec(account_id="paper-default", genesis_collateral=Decimal("1000"))

    assert spec.netting is Netting.NET
    assert spec.account_id == "paper-default"
    assert spec.genesis_collateral == Decimal("1000")
