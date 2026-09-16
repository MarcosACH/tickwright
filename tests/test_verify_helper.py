"""The ``/verify-tickwright`` helper's recorded verdicts (issue #346).

The helper is agent tooling under ``.claude/skills/``, not engine code, but it is
what turns a verification run into a verdict a human files a bug from. These
tests hold the verbs that produce that verdict: ``check`` records, ``report``
summarises, ``runs`` indexes, ``issue-draft`` drafts. They never spawn the
engine: a run directory is built by hand, the way a finished life leaves it.

``VERIFY_HOME`` redirects the run tree to ``tmp_path`` so no test touches
``.agents/verify/`` in the checkout.
"""

import importlib.util
import json
import sqlite3
import sys
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from types import ModuleType

import pytest

_HELPER = Path(__file__).resolve().parents[1] / ".claude/skills/verify-tickwright/scripts/verify.py"


@pytest.fixture
def helper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setenv("VERIFY_HOME", str(tmp_path / "verify"))
    spec = importlib.util.spec_from_file_location("verify_helper", _HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def run(
    helper: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Callable[..., tuple[int, str]]:
    """Call the helper as the shell would: ``run("init", "r1")`` -> (exit code, stdout)."""

    def _run(*argv: str) -> tuple[int, str]:
        monkeypatch.setattr(sys, "argv", ["verify", *argv])
        try:
            code = helper.main()
        except SystemExit as exc:
            code = int(exc.code) if isinstance(exc.code, int) else 1
        return code, capsys.readouterr().out

    return _run


def _finished_life(
    helper: ModuleType,
    run_id: str,
    life: str,
    *,
    exit_code: int,
    events: list[dict],
    orders: list[tuple[str, str]],
) -> None:
    """Leave behind what one life leaves: a log, an exit file, a SQLite store."""
    evidence = helper.evidence(run_id)
    scratch = helper.scratch(run_id)
    (evidence / f"{life}.stderr.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    (evidence / f"{life}.exit").write_text(f"{exit_code}\n")
    (scratch / ".env").write_text("TICKWRIGHT_SQLITE__PATH=store.db\n")
    with closing(sqlite3.connect(scratch / "store.db")) as conn:
        conn.execute("create table if not exists orders (cloid text primary key, state text)")
        conn.executemany("insert or replace into orders values (?, ?)", orders)
        conn.execute(
            "create table if not exists positions (strategy_id text, symbol text, signed_size text)"
        )
        conn.execute("insert into positions values ('shooter', 'BTC', '0.500')")
        conn.commit()


class TestCheck:
    def test_a_passing_sql_check_records_pass_with_both_values(
        self, helper: ModuleType, run: Callable[..., tuple[int, str]]
    ) -> None:
        run("init", "r1")
        _finished_life(helper, "r1", "first", exit_code=0, events=[], orders=[("0xabc", "filled")])

        code, out = run(
            "check",
            "r1",
            "first",
            "paper-fill",
            "--sql",
            "select state from orders where cloid='0xabc'",
            "--expect",
            "filled",
        )

        assert code == 0
        assert (
            helper.evidence("r1") / "checks.txt"
        ).read_text() == "PASS paper-fill first expected=filled got=filled\n"

    def test_a_failing_sql_check_records_fail_and_exits_one(
        self, helper: ModuleType, run: Callable[..., tuple[int, str]]
    ) -> None:
        run("init", "r1")
        _finished_life(helper, "r1", "second", exit_code=0, events=[], orders=[])

        code, _ = run(
            "check",
            "r1",
            "second",
            "crash-converge",
            "--sql",
            "select signed_size from positions",
            "--expect",
            "1.000",
        )

        assert code == 1
        assert (
            helper.evidence("r1") / "checks.txt"
        ).read_text() == "FAIL crash-converge second expected=1.000 got=0.500\n"

    def test_an_exit_check_reads_the_exit_file(
        self, helper: ModuleType, run: Callable[..., tuple[int, str]]
    ) -> None:
        run("init", "r1")
        _finished_life(helper, "r1", "first", exit_code=1, events=[], orders=[])

        code, _ = run("check", "r1", "first", "refuse-genesis", "--exit", "--expect", "1")

        assert code == 0
        assert (
            "PASS refuse-genesis first expected=1 got=1"
            in (helper.evidence("r1") / "checks.txt").read_text()
        )

    def test_an_event_check_counts_occurrences(
        self, helper: ModuleType, run: Callable[..., tuple[int, str]]
    ) -> None:
        run("init", "r1")
        _finished_life(
            helper,
            "r1",
            "first",
            exit_code=0,
            events=[{"event": "order.placed"}, {"event": "order.placed"}],
            orders=[],
        )

        code, _ = run(
            "check", "r1", "first", "paper-restart", "--event", "order.placed", "--expect", "0"
        )

        assert code == 1
        assert (
            "FAIL paper-restart first expected=0 got=2"
            in (helper.evidence("r1") / "checks.txt").read_text()
        )

    def test_checks_accumulate_in_order(
        self, helper: ModuleType, run: Callable[..., tuple[int, str]]
    ) -> None:
        run("init", "r1")
        _finished_life(helper, "r1", "first", exit_code=0, events=[], orders=[])
        run("check", "r1", "first", "a", "--exit", "--expect", "0")
        run("check", "r1", "first", "b", "--exit", "--expect", "9")

        lines = (helper.evidence("r1") / "checks.txt").read_text().splitlines()

        assert [line.split()[0:2] for line in lines] == [["PASS", "a"], ["FAIL", "b"]]
