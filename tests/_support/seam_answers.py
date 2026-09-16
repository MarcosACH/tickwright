"""The gate over whether every configured adapter answered the seam gates.

The three shared gates over the adapter seams are opt-in imports, and each is
called by the adapter's own suite: ``seam_claims`` for a claim per member,
``feed_contract`` for the mark a feed owes, ``lifecycle_contract`` for going
quiet once stopped. Every one of them is real on the adapters that call it and
blind to an adapter that does not. A third venue that shipped a feed, an
exchange, a config and a suite that never imported ``_support`` was green on
the whole merge gate while asserting none of it (#303). ``docs/extending.md``
carried the obligation as a checklist, which is the instrument #227 already
found insufficient for the mark.

**What is discovered is the config, not the tree.** ``AppConfig.feed`` and
``AppConfig.exchange`` are closed ``Literal``s, and an adapter not named there
cannot be selected by ``build_*``, so it is not a runtime adapter yet and owes
nothing. The set is read from the ``Literal``, not from ``venues/*/``, because
``ReplayFeed`` and ``PaperExchange`` live under ``adapters/`` and owe the same
answers. A map from each value to the adapter's name and suite is the one line
a venue author writes here, and the map is checked against the ``Literal`` in
both directions so it cannot go stale silently.

**Answers are read from the source, not registered at runtime**, the same way
``seam_claims._test_names`` reads its suite. A runtime registry would see only
the gate calls pytest reached, so ``-k``, a single-file run, or a live-marked
test skipped by default would all read as an adapter that never answered. The
``ast`` read keys on the adapter's name, so the fake feed
``test_feed_contract.py`` drives the mark gate with does not count for anyone.

The lifecycle gate is owed unconditionally. It refuses an empty transcript, so
an adapter whose ``run()`` publishes nothing cannot answer it today. All four
shipped adapters publish from ``run()``, so the case is open rather than
decided, and the first silent adapter will need a clause on that helper, not an
opt-out here.

Explicit assertion messages throughout: this module is not a test module, so
pytest does not rewrite its asserts and a bare comparison would fail blind.
"""

import ast
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from tickwright.domain import Exchange, MarketFeed


@dataclass(frozen=True)
class Answered:
    """Where one configured adapter answers its gates, and under what name.

    ``adapter`` is the class name the behavioural gates are called with
    (``feed="ReplayFeed"``, ``name="PaperExchange"``). The gates take a free
    string, and this map is what makes that string load-bearing.
    """

    adapter: str
    suite: Path


@dataclass(frozen=True)
class _Gate:
    """One shared gate, and how a call to it names its subject.

    ``keyword`` is the argument that carries the adapter's name. ``None`` is
    the claims gate, whose call names the Protocol positionally and the suite
    by ``Path(__file__).parent``, so being in the mapped directory is the whole
    of what it can say.
    """

    function: str
    keyword: str | None


_CLAIMS = _Gate("assert_every_member_is_claimed", keyword=None)
_MARKED = _Gate("assert_every_traded_symbol_is_marked", keyword="feed")
_QUIET = _Gate("assert_quiet_once_stopped", keyword="name")

_OWED: Mapping[type, tuple[_Gate, ...]] = {
    MarketFeed: (_CLAIMS, _MARKED, _QUIET),
    Exchange: (_CLAIMS, _QUIET),
}


def assert_every_adapter_answers_its_gates(
    seam: type, *, configured: Iterable[str], answers: Mapping[str, Answered]
) -> None:
    """Assert every ``configured`` adapter of ``seam`` answers every gate it owes.

    ``configured`` is the ``Literal``'s values. ``answers`` maps each to where
    its suite answers, and is checked against the ``Literal`` in both
    directions first: a value with no row is the stale-map failure this guard
    exists for, and a row with no value is a suite nobody can select any more.
    Then a missing answer fails naming the adapter and the gate, which is what
    an author adding a venue needs to read.
    """
    configured = set(configured)
    unmapped = configured - set(answers)
    assert not unmapped, (
        f"{seam.__name__} values in AppConfig with no row in the answers map: "
        f"{sorted(unmapped)} — add where each adapter's suite answers its gates"
    )
    stale = set(answers) - configured
    assert not stale, (
        f"rows in the answers map for {seam.__name__} values AppConfig no longer has: "
        f"{sorted(stale)}"
    )
    for value in sorted(configured):
        answered = answers[value]
        unanswered = [
            gate.function
            for gate in _OWED[seam]
            if not _answers(answered, gate, protocol=seam.__name__)
        ]
        assert not unanswered, (
            f"{answered.adapter} ({seam.__name__} {value!r}) never answers {unanswered} in "
            f"{answered.suite} — a {seam.__name__} owes every one of them, see "
            f"docs/extending.md"
        )


def _answers(answered: Answered, gate: _Gate, *, protocol: str) -> bool:
    """Whether some ``test_*.py`` in the suite calls ``gate`` for this adapter."""
    for module in sorted(answered.suite.glob("test_*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _name_of(node.func) != gate.function:
                continue
            if gate.keyword is None:
                if node.args and _name_of(node.args[0]) == protocol:
                    return True
                continue
            for kw in node.keywords:
                if kw.arg == gate.keyword and _literal(kw.value) == answered.adapter:
                    return True
    return False


def _name_of(node: ast.expr) -> str | None:
    """The bare or attribute name a call target or argument is written as."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _literal(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None
