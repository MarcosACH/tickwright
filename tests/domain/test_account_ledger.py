"""The ``Account`` aggregate: one cash line, a closed write-set, a write-once
opening declaration (ADR-0042 §3/§4)."""

from decimal import Decimal

import pytest
from venue_doubles import account_state

from tickwright.domain import Account, AccountSpec, LeverageBook, Netting, account_view


def _account(genesis: str = "100000") -> Account:
    return Account(account_id="paper-default", genesis_collateral=Decimal(genesis), genesis_ts_ns=7)


def test_an_account_opens_with_its_cash_line_at_the_genesis_collateral() -> None:
    account = _account("250000")

    assert account.account_id == "paper-default"
    assert account.cash == Decimal("250000")
    assert account.genesis_collateral == Decimal("250000")
    assert account.genesis_ts_ns == 7
    # And it reaches the seam's snapshot, assembled off the aggregate rather
    # than by it: every Tier-2 Σ on that view ranges over positions an
    # ``Account`` knows nothing about (ADR-0035).
    assert account_view(
        account, positions=(), marks={}, leverage=LeverageBook(), specs={}
    ).cash == Decimal("250000")


def test_realized_pnl_accrues_to_the_cash_line() -> None:
    account = _account("1000")

    account.accrue_realized(Decimal("-40"), event_id="0xabc:fill:f1")

    assert account.cash == Decimal("960")


def test_funding_accrues_to_the_cash_line_with_the_venue_s_own_sign() -> None:
    """``cash += funding``, where the fee's rule is ``cash -= fee`` (ADR-0042 §4).

    The two lines' cash rules legitimately differ because their venue source
    fields carry opposite raw signs: a fee mirrors ``fee``, where positive is a
    cost, and funding mirrors ``userFunding.usdc``, where negative is a payment
    made. Each stays faithful to its own source rather than to a forced house
    convention, which is what keeps live's ingest a verbatim read with no flip
    bug to have.

    So both directions are asserted here: negating in the wrong place turns a
    payment into a credit and still passes a one-sided case.
    """
    account = _account("1000")

    account.accrue_funding(Decimal("-10"), event_id="paper-default:BTC:funding:3600000000000")
    assert account.cash == Decimal("990")  # < 0: paid out

    account.accrue_funding(Decimal("4"), event_id="paper-default:BTC:funding:7200000000000")
    assert account.cash == Decimal("994")  # > 0: received


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


def test_a_ledger_opens_at_the_genesis_its_spec_declares() -> None:
    """``open`` reads the opening cash off the spec that carries it — the paper
    venue always declares one, which ADR-0042 §1 demands and never defaults."""
    spec = AccountSpec(account_id="paper-default", genesis_collateral=Decimal("250000"))

    account = Account.open(spec, ts_ns=7)

    assert account.account_id == "paper-default"
    assert account.genesis_collateral == Decimal("250000")
    assert account.cash == Decimal("250000")
    assert account.genesis_ts_ns == 7


def test_a_live_spec_declaring_no_genesis_opens_the_ledger_at_zero() -> None:
    """Live declares ``None`` because its opening state is ingested from the
    venue as ``accountValue − Σ unrealized_pnl`` at the startup barrier, which
    has not run when the ledger is opened (ADR-0042 §6, ADR-0043 §6).

    Zero is what an *unstarted* live ledger reports, not a default standing in
    for a number nobody chose — ADR-0042 §1 rejects the latter, and only for
    paper, where the config validator makes ``None`` unreachable. Tier-1 forbids
    reporting ``None`` here (ADR-0041 §6), so it has to be some number.
    """
    spec = AccountSpec(account_id="hyperliquid-testnet-0xabc", genesis_collateral=None)

    account = Account.open(spec, ts_ns=11)

    assert account.account_id == "hyperliquid-testnet-0xabc"
    assert account.genesis_collateral == Decimal("0")
    assert account.cash == Decimal("0")


def test_a_spec_names_the_declared_versus_ingested_predicate() -> None:
    """ADR-0043 §10's predicate is a fact about which venue is running, so the
    spec that carries the declaration answers it rather than four callers each
    re-deriving a boolean from a ``Decimal | None``.

    Both polarities are asserted because both are read: the genesis seed and the
    store's disagreement refusals turn on the declared side, while the isolated
    lock and the live-only ledger cadence turn on the ingested one.
    """
    paper = AccountSpec(account_id="paper-default", genesis_collateral=Decimal("250000"))
    live = AccountSpec(account_id="hyperliquid-testnet-0xabc", genesis_collateral=None)

    assert paper.declares_genesis is True
    assert live.declares_genesis is False


def test_a_live_ledger_ingests_its_genesis_net_of_unrealized_pnl() -> None:
    """The opening cash of a live account is read from the venue, never
    configured: ``genesis = accountValue − Σ unrealized_pnl`` (ADR-0042 §6).

    The figures are the recorded cross snapshot's (issue #142 §2): a funded
    testnet account holding 0.002 BTC long at 5x, ``accountValue`` 25.9264
    against an unrealized −0.034 — so the cash behind that equity is 25.9604,
    which is the venue's own arithmetic and not this constructor's restated.

    The subtraction is load-bearing rather than tidy: ``accountValue`` is
    *equity* and already contains unrealized PnL, so writing it into the cash
    line would double-count that PnL the instant ``equity = cash + Σ uPnL``
    (ADR-0040 §7) was evaluated.
    """
    spec = AccountSpec(account_id="hyperliquid-testnet-0xabc", genesis_collateral=None)

    account = Account.ingest(spec, account_state("25.9264", "-0.034"), ts_ns=11)

    assert account.account_id == "hyperliquid-testnet-0xabc"
    assert account.genesis_collateral == Decimal("25.9604")
    assert account.cash == Decimal("25.9604")  # nothing has accrued away from it yet
    assert account.genesis_ts_ns == 11


def test_an_account_spec_declares_the_venue_facts_and_defaults_to_net() -> None:
    spec = AccountSpec(account_id="paper-default", genesis_collateral=Decimal("1000"))

    assert spec.netting is Netting.NET
    assert spec.account_id == "paper-default"
    assert spec.genesis_collateral == Decimal("1000")
