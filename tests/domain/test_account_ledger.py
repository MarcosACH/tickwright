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

    account.accrue_realized(Decimal("-40"), event_id="0xabc:fill:f1")

    assert account.cash == Decimal("960")


def test_the_cash_line_accrues_unconditionally_and_dedups_nothing() -> None:
    """``Account`` is the cash line's *writer*, never a second idempotency
    authority — two accruals of one ``event_id`` move it twice.

    The fill path dedups exactly once, at ``Position.apply``: a redelivery books
    nothing, so the realized *delta* the projection accrues is zero and the cash
    line cannot move. A second applied-event set here would guard nothing that
    gatekeeper does not already guard, would shadow the different key funding
    dedups on (ADR-0037), and would silently swallow the fee leg of a fill whose
    realized leg it had already consumed — both ride one ``event_id``. ADR-0043
    §4 rejects a ledger-side applied set outright; the ``hasattr`` assertion
    below is what fails if one is reintroduced when the ledger becomes durable.

    The redelivery guarantee itself is asserted where it is produced, in
    ``tests/engine/test_portfolio.py``.
    """
    account = _account("1000")

    account.accrue_realized(Decimal("250"), event_id="0xabc:fill:f1")
    account.accrue_realized(Decimal("250"), event_id="0xabc:fill:f1")

    assert account.cash == Decimal("1500")
    assert not hasattr(account, "applied_event_ids")


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
