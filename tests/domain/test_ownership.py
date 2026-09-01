"""``SymbolOwnership`` — ADR-0034's disjointness rule, and only the rule.

Its two callers each own a *placement* test: ``tests/app/test_config.py`` pins
that a configured overlap is refused at load, and
``tests/engine/test_strategy_host.py`` that a registered one is refused at
wiring. What neither can reach through its own layer is a claimant colliding
with **two different incumbents** at once — a config strategy declares one
symbol, and the host test would need three registrations to build the same
book — so that is what this file specifies.
"""

from tickwright.domain import SymbolOwnership


def test_a_refusal_names_every_taken_symbol_with_its_own_incumbent() -> None:
    """The remedy is a separate account, so the refusal must say *whose*.

    A claimant overlapping two strategies has two possible moves, and an error
    naming only the symbols — or only the first incumbent — leaves an operator
    to re-derive the ownership map from config to pick one. Sorted by symbol so
    the sentence is stable enough to read twice; the claimant's own uncontested
    symbols stay out, since they are not what has to change.
    """
    book = SymbolOwnership().claim("alpha", symbols=("BTC", "ETH")).claim("beta", symbols=("SOL",))

    refusal = book.refusal("gamma", symbols=("SOL", "BTC", "DOGE"))

    assert refusal is not None
    assert "gamma" in refusal
    assert "BTC (owned by alpha)" in refusal
    assert "SOL (owned by beta)" in refusal
    assert "DOGE" not in refusal
    assert refusal.index("BTC") < refusal.index("SOL")


def test_a_disjoint_claim_refuses_nothing_and_leaves_the_earlier_book_alone() -> None:
    """The value is rebound, never mutated — which is what lets a caller fold.

    ``AppConfig`` reports every offending strategy in one load, so it keeps
    folding past a refusal; that is only sound if a claim produces a new book
    rather than editing the one a refused strategy was measured against.
    """
    first = SymbolOwnership().claim("alpha", symbols=("BTC",))
    second = first.claim("beta", symbols=("ETH",))

    assert first.refusal("beta", symbols=("ETH",)) is None
    assert dict(first.owners) == {"BTC": "alpha"}
    assert dict(second.owners) == {"BTC": "alpha", "ETH": "beta"}
