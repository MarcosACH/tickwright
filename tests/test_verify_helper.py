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
import shutil
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
    """Call the helper as the shell would: ``run("init", "r1")`` -> (exit code, output).

    Output is stdout then stderr, so a refusal's reason is readable too.
    """

    def _run(*argv: str) -> tuple[int, str]:
        monkeypatch.setattr(sys, "argv", ["verify", *argv])
        try:
            code = helper.main()
        except SystemExit as exc:
            code = int(exc.code) if isinstance(exc.code, int) else 1
            if isinstance(exc.code, str):
                print(exc.code, file=sys.stderr)
        captured = capsys.readouterr()
        return code, captured.out + captured.err

    return _run


@pytest.fixture
def bin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A ``PATH`` holding only ``git``. Add a fake ``docker`` to it, or leave it missing."""
    path = tmp_path / "bin"
    path.mkdir()
    git = shutil.which("git")
    assert git is not None
    (path / "git").symlink_to(git)
    monkeypatch.setenv("PATH", str(path))
    return path


def _fake_docker(bin_dir: Path, *, running: bool) -> None:
    """A ``docker`` that says a compose service is running, or not, and accepts the rest."""
    script = bin_dir / "docker"
    answer = "echo abc123" if running else ":"
    script.write_text(f'#!/bin/sh\ncase "$*" in\n  *"ps -q --status running"*) {answer};;\nesac\n')
    script.chmod(0o755)


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


def _run_with_one_fail(helper: ModuleType, run: Callable[..., tuple[int, str]]) -> None:
    """A crash-recovery run the way the map leaves it today: one PASS, one FAIL."""
    run("init", "r1", "--feature", "crash-recovery")
    _finished_life(
        helper,
        "r1",
        "first",
        exit_code=-9,
        events=[{"event": "order.filled"}, {"event": "engine.faulted", "error": "Boom()"}],
        orders=[("0xabc", "filled")],
    )
    run("check", "r1", "first", "crash-durable", "--exit", "--expect", "-9")
    run(
        "check",
        "r1",
        "first",
        "crash-converge",
        "--sql",
        "select signed_size from positions",
        "--expect",
        "1.000",
    )


class TestReport:
    def test_init_records_the_feature(
        self, helper: ModuleType, run: Callable[..., tuple[int, str]]
    ) -> None:
        run("init", "r1", "--feature", "crash-recovery")

        assert helper.read_run_json("r1")["feature"] == "crash-recovery"

    def test_report_opens_with_the_verdict_and_lists_only_the_fails(
        self, helper: ModuleType, run: Callable[..., tuple[int, str]]
    ) -> None:
        _run_with_one_fail(helper, run)

        code, out = run("report", "r1")
        text = (helper.evidence("r1") / "REPORT.md").read_text()
        failed_section = text.split("## Failed checks")[1].split("##")[0]

        assert code == 1
        assert text.startswith("# r1: crash-recovery FAIL (1 of 2 checks failed)")
        assert "FAIL crash-converge first expected=1.000 got=0.500" in failed_section
        assert "crash-durable" not in failed_section
        assert text == out

    def test_report_names_alarm_events_and_exit_codes(
        self, helper: ModuleType, run: Callable[..., tuple[int, str]]
    ) -> None:
        _run_with_one_fail(helper, run)

        run("report", "r1")
        text = (helper.evidence("r1") / "REPORT.md").read_text()
        alarms = text.split("## Alarm events")[1].split("##")[0]

        assert "engine.faulted" in alarms
        assert "order.filled" not in alarms
        assert "first: -9" in text.split("## Exit codes")[1]

    def test_report_with_no_checks_says_so(
        self, helper: ModuleType, run: Callable[..., tuple[int, str]]
    ) -> None:
        run("init", "r1")
        _finished_life(helper, "r1", "first", exit_code=0, events=[], orders=[])

        code, out = run("report", "r1")

        assert code == 1
        assert out.startswith("# r1: (no feature) NO CHECKS")


class TestRuns:
    def test_runs_lists_every_run_with_its_verdict(
        self, helper: ModuleType, run: Callable[..., tuple[int, str]]
    ) -> None:
        _run_with_one_fail(helper, run)
        run("init", "r2", "--feature", "kill-switch")
        _finished_life(helper, "r2", "arm", exit_code=0, events=[], orders=[])
        run("check", "r2", "arm", "ks-trip", "--exit", "--expect", "0")
        run("init", "r3")

        _, out = run("runs")
        rows = {line.split()[0]: line for line in out.splitlines() if line.startswith("r")}

        assert "crash-recovery" in rows["r1"] and "FAIL 1/2" in rows["r1"]
        assert "kill-switch" in rows["r2"] and "PASS 0/1" in rows["r2"]
        assert "NO CHECKS" in rows["r3"]

    def test_prune_keeps_the_newest_n_runs_per_feature(
        self, helper: ModuleType, run: Callable[..., tuple[int, str]]
    ) -> None:
        for run_id, started in (
            ("a1", "2026-01-01T00:00:00+00:00"),
            ("a2", "2026-01-02T00:00:00+00:00"),
            ("a3", "2026-01-03T00:00:00+00:00"),
        ):
            run("init", run_id, "--feature", "kill-switch")
            data = helper.read_run_json(run_id)
            data["started_at"] = started
            helper.write_run_json(run_id, data)
        run("init", "b1", "--feature", "ghost-reconcile")

        run("prune", "--keep", "2")
        left = sorted(p.name for p in helper.VERIFY_HOME.iterdir())

        assert left == ["a2", "a3", "b1"]


class TestIssueDraft:
    def test_draft_follows_the_bug_template_and_names_the_evidence(
        self, helper: ModuleType, run: Callable[..., tuple[int, str]]
    ) -> None:
        _run_with_one_fail(helper, run)

        code, out = run("issue-draft", "r1")

        assert code == 0
        assert out.startswith("## What happens")
        assert "crash-converge" in out and "expected=1.000 got=0.500" in out
        assert "## What should happen" in out and "## Reproduction" in out
        assert "features/crash-recovery.md" in out
        assert "## Environment" in out and "Commit / branch:" in out
        # The paths a reader chases, relative to the repo.
        assert ".agents/verify/r1/evidence/REPORT.md" in out
        assert ".agents/verify/r1/evidence/first.stderr.jsonl" in out
        # Never files it, and says so.
        assert "gh issue create" in out
        assert (helper.evidence("r1") / "ISSUE.md").read_text() == out

    def test_draft_on_a_passing_run_says_there_is_nothing_to_file(
        self, helper: ModuleType, run: Callable[..., tuple[int, str]]
    ) -> None:
        run("init", "r2", "--feature", "kill-switch")
        _finished_life(helper, "r2", "arm", exit_code=0, events=[], orders=[])
        run("check", "r2", "arm", "ks-trip", "--exit", "--expect", "0")

        code, out = run("issue-draft", "r2")

        assert code == 1
        assert "nothing to file" in out


class TestCheckFile:
    def test_a_file_check_compares_the_files_content(
        self, helper: ModuleType, run: Callable[..., tuple[int, str]]
    ) -> None:
        run("init", "r1")
        _finished_life(helper, "r1", "first", exit_code=0, events=[], orders=[])
        (helper.evidence("r1") / "first.offsets.txt").write_text("verify.kafka:0:9\n")

        code, _ = run(
            "check",
            "r1",
            "first",
            "kafka-topic",
            "--file",
            "first.offsets.txt",
            "--expect",
            "verify.kafka:0:9",
        )

        assert code == 0
        assert (
            "PASS kafka-topic first expected=verify.kafka:0:9 got=verify.kafka:0:9"
            in (helper.evidence("r1") / "checks.txt").read_text()
        )


class TestAwait:
    def test_a_sql_await_without_expect_is_refused(
        self, helper: ModuleType, run: Callable[..., tuple[int, str]]
    ) -> None:
        run("init", "r1")
        _finished_life(helper, "r1", "first", exit_code=0, events=[], orders=[])

        code, out = run(
            "await", "r1", "first", "--sql", "select state from orders", "--timeout", "0"
        )

        assert code == 1
        assert "needs --expect" in out

    def test_a_sql_await_does_not_match_an_empty_expect_on_no_rows(
        self, helper: ModuleType, run: Callable[..., tuple[int, str]]
    ) -> None:
        run("init", "r1")
        _finished_life(helper, "r1", "first", exit_code=0, events=[], orders=[])

        code, out = run(
            "await",
            "r1",
            "first",
            "--sql",
            "select state from orders",
            "--expect",
            "",
            "--timeout",
            "0",
        )

        assert code == 1
        assert "TIMEOUT" in out


class TestStart:
    def test_a_duplicate_life_is_refused_before_its_evidence_is_touched(
        self, helper: ModuleType, run: Callable[..., tuple[int, str]]
    ) -> None:
        run("init", "r1")
        _finished_life(helper, "r1", "first", exit_code=0, events=[], orders=[])

        code, out = run("start", "r1", "first", "--preset", "paper-replay")

        assert code == 1
        assert "already exists" in out
        assert not (helper.evidence("r1") / "first.env.txt").exists()

    def test_params_are_recorded_in_the_evidence_env_only(self, helper: ModuleType) -> None:
        scratch_text, evidence_text = helper.life_env(
            "paper-replay", ["TICKWRIGHT_STRATEGIES=[]"], ["HOLD_TICKS=3"]
        )

        assert "TICKWRIGHT_STRATEGIES=[]\n" in scratch_text
        assert "VERIFY_" not in scratch_text
        assert evidence_text.startswith(scratch_text)
        assert "# VERIFY_HOLD_TICKS=3\n" in evidence_text


class TestDocker:
    def test_doctor_reports_a_missing_docker_binary(
        self, helper: ModuleType, run: Callable[..., tuple[int, str]], bin_dir: Path
    ) -> None:
        code, out = run("doctor")

        assert code == 0
        assert "docker    missing" in out

    def test_infra_up_without_docker_is_refused(
        self, helper: ModuleType, run: Callable[..., tuple[int, str]], bin_dir: Path
    ) -> None:
        run("init", "r1")

        code, out = run("infra", "r1", "up", "kafka")

        assert code == 1
        assert "docker" in out and "missing" in out
        assert helper.read_run_json("r1")["infra"] == []

    def test_infra_up_on_a_running_service_is_not_owned(
        self, helper: ModuleType, run: Callable[..., tuple[int, str]], bin_dir: Path
    ) -> None:
        _fake_docker(bin_dir, running=True)
        run("init", "r1")

        code, out = run("infra", "r1", "up", "kafka")

        assert code == 0
        assert "already running" in out
        assert helper.read_run_json("r1")["infra"] == []

    def test_infra_up_on_a_stopped_service_is_owned(
        self, helper: ModuleType, run: Callable[..., tuple[int, str]], bin_dir: Path
    ) -> None:
        _fake_docker(bin_dir, running=False)
        run("init", "r1")

        code, _ = run("infra", "r1", "up", "kafka")

        assert code == 0
        assert helper.read_run_json("r1")["infra"] == ["kafka"]
