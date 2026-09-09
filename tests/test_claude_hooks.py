"""The ``.claude/hooks`` scripts: work done at the tool call, not asked for in prose.

Six hooks across three events, and they come in two shapes.

**Five guards** refuse. ``PreToolUse`` on ``Bash`` (and, for one of them, ``Read`` too):
each reads the event JSON on stdin and answers with an exit code — ``0`` allows the call,
``2`` blocks it and hands the text on stderr back to the agent as the reason.

**Two answer instead.** ``ruff-on-write`` runs on ``PostToolUse``, which cannot block by
design, and fixes the file rather than arguing about it; ``resume-from-plan`` runs on
``SessionStart`` and prints, where stdout becomes context. Neither has a refusal to
assert, so what is asserted is the effect on disk and the text handed back.

That contract is the whole subject here, so nothing is mocked: the hooks are run as real
processes, the way Claude Code runs them. Most of them decide by asking a real tool about
the path — ``git`` (tracked? ignored? which branch?), ``doc-slice`` (what are its
sections?) or ``ruff`` (is this formatted?) — so the fixtures are real scratch repos
rather than stubbed answers, the same shape and the same reason as
``tests/test_githooks.py``. Global and system git config are pinned to ``/dev/null`` so a
developer's own settings cannot reach an outcome.

The hooks are held to the stdlib alone and to ``/usr/bin/env python3``: they run before
``uv sync`` has necessarily happened on a fresh clone, and the project venv is not
reliably on a hook's PATH.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_HOOKS = _ROOT / ".claude" / "hooks"
_RUFF = _ROOT / ".venv" / "bin" / "ruff"

_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}

ALLOW = 0
BLOCK = 2


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, env=_ENV, capture_output=True, check=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A scratch repo carrying one tracked file, one ignored tree, and one plan file.

    ``.gitignore`` mirrors the shape of the real one that matters to these hooks: a
    build/venv tree, a log, and ``.agents/plans/`` — which is ignored *and* meant to be
    read, so it is the exception the read guard has to carry.
    """
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / "logs").mkdir()
    (root / ".agents" / "plans").mkdir(parents=True)

    (root / ".gitignore").write_text(".venv/\nlogs/\n*.log\n.agents/plans/\n.env\n")
    (root / "src" / "tracked.py").write_text("x = 1\n")
    (root / ".venv" / "bin" / "ruff").write_text("#!/bin/sh\n")
    (root / "logs" / "run.log").write_text("noise\n")
    (root / ".agents" / "plans" / "issue-1.md").write_text("- [ ] behavior\n")
    (root / ".env").write_text("TICKWRIGHT_HYPERLIQUID__SIGNING_KEY=0xdead\n")

    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "add", ".gitignore", "src/tracked.py")
    _git(root, "commit", "-q", "-m", "seed")
    return root


@pytest.fixture
def docs_repo(tmp_path: Path) -> Path:
    """A scratch repo carrying one file of each corpus shape, plus two that are not.

    ``doc-slice`` is **copied in at fixture time** rather than committed as a fixture
    file: the guard resolves the tool from the repo root of the call it is judging, so a
    scratch repo needs a real one, and a checked-in copy is the stale-fixture problem
    ``evals/README.md`` warns about. Copying it each run means it cannot drift.

    The research note carries a bare ``**(unverified)**`` — the inline bold form that is
    ordinary prose everywhere outside the append-corrected corpus. ``doc-slice
    --amendments`` exits 3 on it, which is the *out of domain* signal the guard has to
    survive rather than treat as failure.
    """
    root = tmp_path / "docs-repo"
    (root / "docs" / "adr").mkdir(parents=True)
    (root / "docs" / "module-maps").mkdir(parents=True)
    (root / "docs" / "research").mkdir(parents=True)
    (root / "docs" / "agents").mkdir(parents=True)
    (root / ".agents" / "tools").mkdir(parents=True)

    tool = root / ".agents" / "tools" / "doc-slice"
    shutil.copy(_HOOKS.parent.parent / ".agents" / "tools" / "doc-slice", tool)
    tool.chmod(0o755)

    (root / "docs" / "adr" / "0001-a-decision.md").write_text(
        "# ADR-0001: A decision\n\n"
        "## Context\n\nSomething was true.\n\n"
        "## Decision\n\nThe first answer.\n\n"
        "**(Amended by ADR-0002:** the answer is now the second one.**)**\n\n"
        "## Consequences\n\nThey follow.\n"
    )
    (root / "CONTEXT.md").write_text(
        "# Glossary\n\n## Language\n\n"
        "**Engine**:\nThe process that hosts the pipeline.\n\n"
        "**EventBus**:\nThe transport everything couples through.\n\n"
        "## Relationships\n\n- The Engine hosts one EventBus.\n"
    )
    (root / "docs" / "module-maps" / "surface.md").write_text("# A surface\n\n## Module\n\nIt.\n")
    (root / "docs" / "research" / "note.md").write_text(
        "# A note\n\n## Finding\n\nThe venue does this **(unverified)**.\n"
    )
    (root / "docs" / "agents" / "guide.md").write_text("# A guide\n\n## How\n\nLike this.\n")
    (root / "README.md").write_text("# Readme\n\nShort.\n")

    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    return root


@pytest.fixture
def python_repo(tmp_path: Path) -> Path:
    """A scratch repo carrying this project's ruff settings and a ruff to apply them.

    Both are taken from the real tree at fixture time rather than written out here.
    ``pyproject.toml`` is copied because ruff reads its settings from the checked file's
    own tree, so a scratch repo without it would be judged against upstream defaults —
    88 columns rather than this project's 100 — and the test would assert a formatting
    this repo does not use. ``ruff`` is symlinked because the hook resolves the binary at
    ``<repo root>/.venv/bin/ruff``, which is a path a scratch repo has to actually have.

    Neither can go stale, which is the point: the same reasoning as ``docs_repo``'s
    ``doc-slice`` copy, and the alternative ``evals/README.md`` points at.
    """
    root = tmp_path / "py-repo"
    (root / "src").mkdir(parents=True)
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "bin" / "ruff").symlink_to(_RUFF)
    shutil.copy(_ROOT / "pyproject.toml", root / "pyproject.toml")

    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.com")
    return root


_PLAN = """# issue-900 — the leverage bound

## Behavior checklist
- [x] 1. a leverage below 1 is refused                     abc1234/def5678
- [ ] 2. a leverage above `max_leverage` is refused
- [ ] 3. the engine reads the resolved book

## Docs-sync
- [ ] `CLAUDE.md` gains the bound
"""


@pytest.fixture
def ralph_repo(tmp_path: Path) -> Path:
    """A scratch repo mid-slice: on ``ralph/issue-900``, with #900's plan beside it.

    This is the state both fast-feedback hooks read. ``no-unlinked-prs`` derives the
    ``Closes #900`` line it hands back from the branch name; ``resume-from-plan`` reads
    the branch to find the plan and the plan to find what is left. Neither is told the
    number — deriving it is the behavior under test, and it is what makes the pair
    something other than a rule the agent has to remember.
    """
    root = tmp_path / "ralph-repo"
    (root / ".agents" / "plans").mkdir(parents=True)
    (root / "src").mkdir()

    (root / ".gitignore").write_text(".agents/plans/\n")
    (root / "src" / "leverage.py").write_text("x = 1\n")
    (root / ".agents" / "plans" / "issue-900.md").write_text(_PLAN)

    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    _git(root, "checkout", "-q", "-b", "ralph/issue-900")
    return root


def _run_raw(name: str, payload: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run one hook as a real process against whatever bytes are on stdin."""
    return subprocess.run(
        [str(_HOOKS / name)],
        input=payload,
        cwd=cwd,
        env=_ENV,
        capture_output=True,
        text=True,
    )


def _run(name: str, event: dict[str, object], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run one hook against the event JSON Claude Code would send."""
    return _run_raw(name, json.dumps(event), cwd)


def run_tool_hook(
    name: str,
    tool: str,
    tool_input: dict[str, object],
    cwd: Path,
    hook_event: str = "PreToolUse",
) -> subprocess.CompletedProcess[str]:
    """Drive one hook with a tool event. ``PostToolUse`` additionally carries the tool's
    own result, which the runtime sends and a hook is free to ignore."""
    event: dict[str, object] = {
        "hook_event_name": hook_event,
        "tool_name": tool,
        "tool_input": tool_input,
        "cwd": str(cwd),
    }
    if hook_event == "PostToolUse":
        event["tool_response"] = {"success": True}
    return _run(name, event, cwd)


def run_hook(
    name: str, command: str, cwd: Path, tool: str = "Bash"
) -> subprocess.CompletedProcess[str]:
    """Drive one hook with a Bash call — the shape most of the guards judge."""
    return run_tool_hook(name, tool, {"command": command}, cwd)


def run_session_hook(
    name: str, cwd: Path, source: str = "startup"
) -> subprocess.CompletedProcess[str]:
    """Drive one hook with a ``SessionStart`` event, whose stdout becomes context."""
    return _run(name, {"hook_event_name": "SessionStart", "source": source, "cwd": str(cwd)}, cwd)


def context_of(result: subprocess.CompletedProcess[str]) -> str:
    """The text a ``PostToolUse`` hook hands back to the agent, or ``""`` for silence.

    Silence is a real answer and the commonest one, so it is spelled as empty rather than
    raised on: a hook with nothing to say prints nothing at all, which costs no context.
    """
    if not result.stdout.strip():
        return ""
    payload = json.loads(result.stdout)
    return str(payload["hookSpecificOutput"]["additionalContext"])


class TestNoTrackedWrites:
    """A file git already tracks is edited with ``Edit``, never rewritten through Bash.

    The rule keys on *tracked*, which is the mechanical reading of "a file that already
    exists": creating a new file with a heredoc stays allowed, which is the exemption
    ``CLAUDE.md`` already granted in prose.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "sed -i '' 's/x = 1/x = 2/' src/tracked.py",
            "sed -i.bak 's/x/y/' src/tracked.py",
            "sed --in-place 's/x/y/' src/tracked.py",
            "printf 'x = 2\\n' > src/tracked.py",
            "cat <<'PY' > src/tracked.py\nx = 2\nPY",
            "echo 'x = 2' >> src/tracked.py",
            "echo 'x = 2' | tee src/tracked.py",
            "echo 'x = 2' | tee -a src/tracked.py",
        ],
    )
    def test_a_write_at_a_tracked_path_is_refused(self, repo: Path, command: str) -> None:
        result = run_hook("no-tracked-writes.py", command, repo)
        assert result.returncode == BLOCK
        assert "src/tracked.py" in result.stderr

    @pytest.mark.parametrize(
        "command",
        [
            # Creating a file — nothing to stale, and the exemption the prose granted.
            "cat <<'PY' > src/brand_new.py\nx = 1\nPY",
            "printf 'x\\n' > src/brand_new.py",
            "sed -i '' 's/x/y/' src/brand_new.py",
            # Scratch space outside the repo is not the subject at all.
            "printf 'x\\n' > /tmp/scratch.txt",
            # A redirect that names no file: the commonest false positive there is.
            "uv run pytest -q 2>&1 | tail -5",
            "ls src/ 2>/dev/null",
            # A read of the tracked file is not a write.
            "grep -n 'x' src/tracked.py",
        ],
    )
    def test_a_write_that_stales_nothing_is_allowed(self, repo: Path, command: str) -> None:
        assert run_hook("no-tracked-writes.py", command, repo).returncode == ALLOW

    def test_another_tool_is_not_this_hooks_business(self, repo: Path) -> None:
        """The matcher narrows to Bash, but a hook that trusts its matcher is a hook
        that misfires the day the matcher is widened."""
        result = run_hook(
            "no-tracked-writes.py", "sed -i '' 's/x/y/' src/tracked.py", repo, tool="Read"
        )
        assert result.returncode == ALLOW

    def test_the_reason_names_edit(self, repo: Path) -> None:
        """Exit 2 hands stderr back to the agent as the block reason, so the text is the
        hook's only chance to say what to do instead."""
        result = run_hook("no-tracked-writes.py", "sed -i '' 's/x/y/' src/tracked.py", repo)
        assert "Edit" in result.stderr

    def test_the_hook_is_executable(self) -> None:
        """Claude Code execs the file directly; a lost mode bit is a silently dead guard."""
        assert os.access(_HOOKS / "no-tracked-writes.py", os.X_OK)


class TestNoExcludedReads:
    """What `.claude/settings.json` denies to `Read`, Bash may not fetch either.

    The deny list binds one tool. `cat`, `head` and `grep` reach the same bytes, and
    bypass-permissions mode is what pushes an agent toward Bash in the first place — so
    the deny list guards the door the agent is being told not to use.

    The membership test is `git check-ignore`, not a second path list. `.gitignore`
    already names every tree this repo keeps out of context, and two lists that must
    agree are the drift this project calls a bug.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "cat .venv/bin/ruff",
            "head -20 .venv/bin/ruff",
            "tail -f logs/run.log",
            "less logs/run.log",
            "grep -n 'noise' logs/run.log",
            "wc -l .venv/bin/ruff",
            # Secrets are ignored for a stronger reason than context budget, and the one
            # rule covers both.
            "cat .env",
        ],
    )
    def test_a_read_of_an_excluded_path_is_refused(self, repo: Path, command: str) -> None:
        assert run_hook("no-excluded-reads.py", command, repo).returncode == BLOCK

    @pytest.mark.parametrize(
        "command",
        [
            # The plan file is ignored on purpose and reading it is the whole point of
            # the convention, so the one exception the guard carries.
            "cat .agents/plans/issue-1.md",
            # Repo source is the normal case and must stay cheap.
            "cat src/tracked.py",
            "grep -rn 'x' src/",
            # An ignored path in the *executable* position is a program being run, not a
            # file being read — and running the venv binaries directly is what keeps a
            # PostToolUse hook fast enough to exist.
            ".venv/bin/ruff check src/",
            ".venv/bin/pytest -q",
            # A reader with no path at all.
            "cat",
        ],
    )
    def test_a_read_that_costs_no_context_is_allowed(self, repo: Path, command: str) -> None:
        assert run_hook("no-excluded-reads.py", command, repo).returncode == ALLOW

    def test_the_reason_names_the_path_and_why(self, repo: Path) -> None:
        result = run_hook("no-excluded-reads.py", "cat logs/run.log", repo)
        assert "logs/run.log" in result.stderr
        assert ".gitignore" in result.stderr

    def test_a_reader_downstream_of_a_pipe_is_still_a_reader(self, repo: Path) -> None:
        """Segmenting on the pipe is what keeps the executable-position exemption honest;
        without it, everything after the first command reads as an argument."""
        result = run_hook("no-excluded-reads.py", "echo x | cat logs/run.log", repo)
        assert result.returncode == BLOCK

    def test_another_tool_is_not_this_hooks_business(self, repo: Path) -> None:
        assert run_hook("no-excluded-reads.py", "cat .env", repo, tool="Read").returncode == ALLOW

    def test_a_newline_ends_a_command_as_surely_as_a_pipe(self, repo: Path) -> None:
        """A multi-line script is many commands, and the lexer drops newlines as ordinary
        whitespace — so without an explicit split, line two's program reads as an argument
        to line one's. This guard found it in its own repo on the first live call: a
        `tail -1` ending one line swallowed the `.venv/bin/pytest` opening the next.
        """
        assert (
            run_hook(
                "no-excluded-reads.py", "echo a | tail -1\n.venv/bin/pytest -q", repo
            ).returncode
            == ALLOW
        )

    def test_a_read_on_a_later_line_is_still_a_read(self, repo: Path) -> None:
        """The other half of the same fix: splitting on newlines must not lose the lines."""
        assert (
            run_hook("no-excluded-reads.py", "echo a\ncat logs/run.log", repo).returncode == BLOCK
        )

    def test_the_hook_is_executable(self) -> None:
        assert os.access(_HOOKS / "no-excluded-reads.py", os.X_OK)


class TestNoGlobalInstalls:
    """Dependencies land in the project venv through `uv`, never in the system one.

    The rule is the maintainer's, stated once at the user level and nowhere enforced.
    It is worth a guard rather than a paragraph because the damage is off-repo: a
    package installed into the system Python survives the branch, the PR and the
    revert, and nothing in this repo's gate can see that it happened.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "pip install httpx",
            "pip3 install httpx",
            "python -m pip install httpx",
            "python3 -m pip install --upgrade httpx",
            "uv pip install --system httpx",
            "uv tool install ruff",
            "pipx install ruff",
            "brew install jq",
            "npm install -g typescript",
            "npm i -g typescript",
            "npm install --global typescript",
        ],
    )
    def test_an_install_outside_the_project_venv_is_refused(self, repo: Path, command: str) -> None:
        assert run_hook("no-global-installs.py", command, repo).returncode == BLOCK

    @pytest.mark.parametrize(
        "command",
        [
            # The sanctioned path: uv against the project venv and its lockfile.
            "uv add httpx",
            "uv add --dev pytest-xdist",
            "uv sync --frozen --dev",
            "uv run pytest -q",
            # Ephemeral, isolated, and installs nothing durable — the CI dependency
            # audit is exactly this shape.
            "uvx yt-dlp --version",
            "uv tool run pip-audit --requirement /tmp/requirements.txt",
            # venv-local by construction, so outside the rule even though it says pip.
            "uv pip install httpx",
            ".venv/bin/pip install httpx",
            # An install that is local to a project, not to the machine.
            "npm install",
            # Not an install at all.
            "pip --version",
            "brew list",
        ],
    )
    def test_an_install_into_the_project_is_allowed(self, repo: Path, command: str) -> None:
        assert run_hook("no-global-installs.py", command, repo).returncode == ALLOW

    def test_the_reason_names_the_sanctioned_command(self, repo: Path) -> None:
        result = run_hook("no-global-installs.py", "pip install httpx", repo)
        assert "uv add" in result.stderr

    def test_an_install_on_a_later_line_is_still_an_install(self, repo: Path) -> None:
        """Same newline bug as the read guard, and worse here: this one fails *open*, so
        a multi-line script would have carried a global install straight through."""
        assert (
            run_hook("no-global-installs.py", "echo a\npip install httpx", repo).returncode == BLOCK
        )

    def test_the_hook_is_executable(self) -> None:
        assert os.access(_HOOKS / "no-global-installs.py", os.X_OK)


class TestNoUnslicedDocReads:
    """The long-form corpus is read by section, and a whole read has to be asked for.

    Membership is a glob list rather than a git predicate, unlike the two path guards:
    "long enough to be worth slicing" is editorial and git has no opinion on it. What is
    fenced instead is that the list still names real files —
    ``test_every_corpus_glob_matches_a_real_file`` — so a renamed directory fails here
    rather than silently disarming the guard.
    """

    @pytest.mark.parametrize(
        "path",
        [
            "docs/adr/0001-a-decision.md",
            "CONTEXT.md",
            "docs/module-maps/surface.md",
            "docs/research/note.md",
        ],
    )
    def test_a_whole_read_of_a_corpus_file_is_refused(self, docs_repo: Path, path: str) -> None:
        result = run_tool_hook("no-unsliced-doc-reads.py", "Read", {"file_path": path}, docs_repo)
        assert result.returncode == BLOCK
        assert path in result.stderr

    @pytest.mark.parametrize(
        "path", ["README.md", "docs/agents/guide.md", ".agents/tools/doc-slice"]
    )
    def test_a_file_outside_the_corpus_is_read_whole(self, docs_repo: Path, path: str) -> None:
        """The corpus is four globs, not "documentation". ``docs/agents/`` is workflow
        prose read end to end on purpose, and a guard that reached it would be charging
        for the cheap files to protect the expensive ones."""
        result = run_tool_hook("no-unsliced-doc-reads.py", "Read", {"file_path": path}, docs_repo)
        assert result.returncode == ALLOW

    @pytest.mark.parametrize(
        "tool_input",
        [
            {"file_path": "docs/adr/0001-a-decision.md", "offset": 5},
            {"file_path": "docs/adr/0001-a-decision.md", "limit": 40},
            {"file_path": "docs/adr/0001-a-decision.md", "offset": 5, "limit": 40},
            {"file_path": "docs/adr/0001-a-decision.md", "offset": 1, "limit": 99999},
        ],
    )
    def test_an_explicit_offset_or_limit_is_allowed(
        self, docs_repo: Path, tool_input: dict[str, object]
    ) -> None:
        """Including the last one, which reads the file whole.

        That is the escape, not a hole in the guard. The rule being enforced is that a
        whole read is a **deliberate** act rather than the default shape of the call, and
        a guard with no deliberate escape is one an agent learns to route around — the
        same reasoning that makes all four of these fail open on input they cannot parse.
        """
        result = run_tool_hook("no-unsliced-doc-reads.py", "Read", tool_input, docs_repo)
        assert result.returncode == ALLOW

    def test_an_absolute_path_is_judged_the_same_as_a_relative_one(self, docs_repo: Path) -> None:
        result = run_tool_hook(
            "no-unsliced-doc-reads.py",
            "Read",
            {"file_path": str(docs_repo / "docs" / "adr" / "0001-a-decision.md")},
            docs_repo,
        )
        assert result.returncode == BLOCK

    def test_a_path_outside_the_repo_is_allowed(self, docs_repo: Path, tmp_path: Path) -> None:
        elsewhere = tmp_path / "docs" / "adr" / "0001-somebody-elses.md"
        elsewhere.parent.mkdir(parents=True)
        elsewhere.write_text("# Not ours\n")
        result = run_tool_hook(
            "no-unsliced-doc-reads.py", "Read", {"file_path": str(elsewhere)}, docs_repo
        )
        assert result.returncode == ALLOW

    def test_a_corpus_path_that_does_not_exist_is_allowed(self, docs_repo: Path) -> None:
        """There is nothing to slice, so the refusal would have no index to offer and the
        `Read` is about to report the real problem anyway."""
        result = run_tool_hook(
            "no-unsliced-doc-reads.py", "Read", {"file_path": "docs/adr/0099-absent.md"}, docs_repo
        )
        assert result.returncode == ALLOW

    def test_the_refusal_carries_the_table_of_contents(self, docs_repo: Path) -> None:
        """A bare "no" costs the agent a turn to recover from and teaches it to argue.

        The refusal hands back the index the caller was going to need anyway, so the
        blocked call resolves in one more turn rather than two — and complying stops
        being the expensive option, which is the whole reason the rule needed a guard.
        """
        result = run_tool_hook(
            "no-unsliced-doc-reads.py",
            "Read",
            {"file_path": "docs/adr/0001-a-decision.md"},
            docs_repo,
        )
        assert result.returncode == BLOCK
        for heading in ("Context", "Decision", "Consequences"):
            assert heading in result.stderr

    def test_a_corrected_section_is_marked_in_that_table_of_contents(self, docs_repo: Path) -> None:
        """``docs/adr/`` is append-corrected, so the amendment blocks hold the current
        truth and the prose above them is often the retired version. Marking which
        sections carry one turns the reading order from a rule the agent has to remember
        into a fact it is handed."""
        result = run_tool_hook(
            "no-unsliced-doc-reads.py",
            "Read",
            {"file_path": "docs/adr/0001-a-decision.md"},
            docs_repo,
        )
        marked = [line for line in result.stderr.splitlines() if "(+1)" in line]
        assert [line for line in marked if "Decision" in line], result.stderr
        assert not [line for line in marked if "Context" in line], result.stderr

    def test_a_file_outside_the_amendment_convention_still_gets_its_contents(
        self, docs_repo: Path
    ) -> None:
        """``doc-slice --amendments`` exits 3 on the research notes: there ``**(`` is
        ordinary bold prose and opens a block that never closes. That exit means *out of
        domain*, not malformed, so the guard drops the annotation and keeps the TOC —
        treating it as a failure would refuse the read with nothing to offer."""
        result = run_tool_hook(
            "no-unsliced-doc-reads.py", "Read", {"file_path": "docs/research/note.md"}, docs_repo
        )
        assert result.returncode == BLOCK
        assert "Finding" in result.stderr
        assert "(+" not in result.stderr

    def test_the_glossary_is_indexed_by_term_rather_than_by_heading(self, docs_repo: Path) -> None:
        """``CONTEXT.md``'s units are bold terms, not headings — its ``Language`` section
        is one h2 running 770 of the real file's 810 lines. A table of contents of it is
        therefore not an index of it, and a refusal offering one would send the agent to
        ``doc-slice CONTEXT.md Language``, which returns the file it was just refused.

        The term lines are the index: 45 of them, 1,086 characters against 61,122. Each
        carries its line number, so the follow-up is the ``offset``/``limit`` Read this
        guard already allows.
        """
        result = run_tool_hook(
            "no-unsliced-doc-reads.py", "Read", {"file_path": "CONTEXT.md"}, docs_repo
        )
        assert result.returncode == BLOCK
        assert "Engine" in result.stderr
        assert "EventBus" in result.stderr
        assert "offset" in result.stderr

    def test_only_the_glossary_is_indexed_by_term(self, docs_repo: Path) -> None:
        """The exception is named, not inferred. An ADR's headings *are* its units, and
        scanning it for bold-prefixed lines would index its emphasis."""
        result = run_tool_hook(
            "no-unsliced-doc-reads.py",
            "Read",
            {"file_path": "docs/adr/0001-a-decision.md"},
            docs_repo,
        )
        assert "Amended by ADR-0002" not in result.stderr

    def test_a_repo_with_no_doc_slice_is_not_blocked(self, docs_repo: Path) -> None:
        """Fail open, for a reason narrower than the usual one: without the tool there is
        no index to answer with, and a refusal that offers nothing is an obstacle rather
        than a guard. ``TestWiring`` is what keeps the real tool from going missing."""
        (docs_repo / ".agents" / "tools" / "doc-slice").unlink()
        result = run_tool_hook(
            "no-unsliced-doc-reads.py",
            "Read",
            {"file_path": "docs/adr/0001-a-decision.md"},
            docs_repo,
        )
        assert result.returncode == ALLOW

    @pytest.mark.parametrize(
        "command",
        [
            "cat docs/adr/0001-a-decision.md",
            "bat CONTEXT.md",
            "less docs/module-maps/surface.md",
            "more docs/research/note.md",
            "nl docs/adr/0001-a-decision.md",
            "strings docs/adr/0001-a-decision.md",
            "cat docs/adr/0001-a-decision.md | head -40",
        ],
    )
    def test_a_shell_dump_of_a_corpus_file_is_refused(self, docs_repo: Path, command: str) -> None:
        """A guard bound to ``Read`` alone proves nothing: ``cat`` loads the identical
        bytes through Bash, and the eval case this guard replaces graded both doors for
        exactly that reason. The last one is the shape that makes it obvious — piping a
        whole file into ``head`` still spends the whole file first."""
        result = run_hook("no-unsliced-doc-reads.py", command, docs_repo)
        assert result.returncode == BLOCK

    @pytest.mark.parametrize(
        "command",
        [
            # Already the slice the rule asks for. Bounding a read is compliance, not evasion.
            "head -40 docs/adr/0001-a-decision.md",
            "tail -20 CONTEXT.md",
            "sed -n '1,40p' docs/adr/0001-a-decision.md",
            "grep -n 'leverage' docs/adr/0001-a-decision.md",
            "rg 'leverage' docs/module-maps/surface.md",
            "wc -l docs/adr/0001-a-decision.md",
            # The sanctioned path itself.
            ".agents/tools/doc-slice docs/adr/0001-a-decision.md Decision",
            ".agents/tools/doc-slice --amendments docs/adr/0001-a-decision.md",
            # Outside the corpus.
            "cat README.md",
            "cat docs/agents/guide.md",
            # Not a read of it at all.
            "git log --oneline -- docs/adr/0001-a-decision.md",
            "ls docs/adr/",
        ],
    )
    def test_a_bounded_read_of_a_corpus_file_is_allowed(
        self, docs_repo: Path, command: str
    ) -> None:
        result = run_hook("no-unsliced-doc-reads.py", command, docs_repo)
        assert result.returncode == ALLOW

    def test_a_dump_on_a_later_line_is_still_a_dump(self, docs_repo: Path) -> None:
        """The regression the first three guards found on their own first live call: the
        lexer drops newlines like any other space, so without a per-line split a dumper
        opening line two reads as an argument to whatever ended line one."""
        result = run_hook(
            "no-unsliced-doc-reads.py",
            "git status --porcelain\ncat docs/adr/0001-a-decision.md",
            docs_repo,
        )
        assert result.returncode == BLOCK

    def test_a_refused_dump_gets_the_same_index_a_refused_read_does(self, docs_repo: Path) -> None:
        result = run_hook("no-unsliced-doc-reads.py", "cat docs/adr/0001-a-decision.md", docs_repo)
        assert "Consequences" in result.stderr
        assert "(+1)" in result.stderr

    def test_the_hook_is_executable(self) -> None:
        assert os.access(_HOOKS / "no-unsliced-doc-reads.py", os.X_OK)


class TestNoUnlinkedPrs:
    """A PR that closes nothing is caught at `gh pr create`, not by a red check.

    ``pr-policy``'s *Body closes an issue* step is right and stays the floor. It just
    reports late: the PR exists by then, the check is red, and clearing it costs a
    ``gh pr edit`` and a re-run. The branch already names the issue, so the missing line
    is derivable rather than merely detectable — which is why the refusal hands back the
    exact text instead of naming the rule.
    """

    def test_a_body_that_closes_nothing_is_refused(self, ralph_repo: Path) -> None:
        result = run_hook(
            "no-unlinked-prs.py",
            'gh pr create --title "feat: a thing" --body "It does the thing."',
            ralph_repo,
        )
        assert result.returncode == BLOCK

    def test_the_refusal_carries_the_line_the_branch_implies(self, ralph_repo: Path) -> None:
        """Answer, don't just refuse. ``ralph/issue-900`` is the whole derivation, so the
        agent gets the text to paste rather than a rule to look up."""
        result = run_hook(
            "no-unlinked-prs.py",
            'gh pr create --title "feat: a thing" --body "It does the thing."',
            ralph_repo,
        )
        assert "Closes #900" in result.stderr

    @pytest.mark.parametrize(
        "body",
        [
            "Closes #900",
            "closes #900",
            "Fixes #900",
            "fixed #900",
            "Resolves #900",
            "Does the thing.\n\nCloses #900\n",
        ],
    )
    def test_a_body_that_closes_an_issue_is_allowed(self, ralph_repo: Path, body: str) -> None:
        """Every spelling GitHub honours, because the hook holds ``pr-policy``'s pattern
        rather than this project's narrower house style. A hook stricter than the check
        it front-runs refuses bodies that would have passed, which is a false refusal."""
        command = f'gh pr create --title "t" --body {json.dumps(body)}'
        assert run_hook("no-unlinked-prs.py", command, ralph_repo).returncode == ALLOW

    @pytest.mark.parametrize(
        "command",
        [
            'gh pr create -b "no reference"',
            'gh pr create --body="no reference"',
            'gh -R MarcosACH/tickwright pr create --assignee @me --body "no reference"',
            'git push -u origin HEAD\ngh pr create --title "t" --body "no reference"',
        ],
    )
    def test_every_shape_the_body_arrives_in_is_read(self, ralph_repo: Path, command: str) -> None:
        """The short flag, the ``=`` form, a flag ahead of the subcommand, and the
        multi-line script — the last of which is the regression #307 found live, where
        the lexer discards newlines and a whole script reads as one segment."""
        assert run_hook("no-unlinked-prs.py", command, ralph_repo).returncode == BLOCK

    def test_a_body_file_is_read_and_judged(self, ralph_repo: Path) -> None:
        (ralph_repo / "body.md").write_text("It does the thing.\n")
        result = run_hook("no-unlinked-prs.py", "gh pr create -F body.md", ralph_repo)
        assert result.returncode == BLOCK
        assert "Closes #900" in result.stderr

    def test_a_body_file_that_closes_is_allowed(self, ralph_repo: Path) -> None:
        (ralph_repo / "body.md").write_text("It does the thing.\n\nCloses #900\n")
        command = "gh pr create --body-file body.md"
        assert run_hook("no-unlinked-prs.py", command, ralph_repo).returncode == ALLOW

    @pytest.mark.parametrize(
        "command",
        [
            # No body this hook can see: gh builds one from the commits, or opens an
            # editor, or hands the whole thing to a browser. Judging what is not there
            # is guessing, and a guard that guesses gets routed around.
            'gh pr create --title "t" --fill',
            'gh pr create --title "t" --fill-verbose',
            "gh pr create --web",
            'gh pr create --title "t"',
            "gh pr create -F -",
            "gh pr create -F missing.md",
            # Not the subject at all.
            'gh pr edit 310 --body "no reference"',
            'gh issue create --title "t" --body "no reference"',
            "gh pr view 310",
            'echo "gh pr create --body nothing"',
        ],
    )
    def test_a_body_this_hook_cannot_read_is_not_one_it_may_refuse(
        self, ralph_repo: Path, command: str
    ) -> None:
        assert run_hook("no-unlinked-prs.py", command, ralph_repo).returncode == ALLOW

    def test_a_branch_that_names_no_issue_is_still_refused(self, ralph_repo: Path) -> None:
        """The rule is the body, not the branch. Off a ``ralph/issue-<N>`` branch there is
        no number to hand back, so the refusal says what is missing without inventing
        one — a fabricated number is worse than none."""
        _git(ralph_repo, "checkout", "-q", "-b", "spike/try-something")
        result = run_hook(
            "no-unlinked-prs.py", 'gh pr create --title "t" --body "nothing"', ralph_repo
        )
        assert result.returncode == BLOCK
        assert "Closes #" in result.stderr
        assert "#900" not in result.stderr

    def test_another_tool_is_not_this_hooks_business(self, ralph_repo: Path) -> None:
        result = run_tool_hook(
            "no-unlinked-prs.py",
            "Read",
            {"file_path": str(ralph_repo / "src" / "leverage.py")},
            ralph_repo,
        )
        assert result.returncode == ALLOW

    def test_an_unreadable_event_is_not_one_to_block_on(self, ralph_repo: Path) -> None:
        assert _run_raw("no-unlinked-prs.py", "{oops", ralph_repo).returncode == ALLOW


class TestRuffOnWrite:
    """The formatter runs at the edit, not at the pull request.

    ``ci`` runs ``ruff format --check .`` and ``ruff check .`` and **reports only** —
    neither auto-fixes, so one formatting slip costs a red run, a fix commit and a second
    run. The fix is mechanical and the tool is already in ``.venv``. This closes that
    window: after every ``Edit`` or ``Write`` of a Python file, ruff formats and fixes it
    in place, and the hook says what it changed.

    ``PostToolUse`` cannot block, and that is the right event rather than a limitation —
    the write already happened and the point is to correct it, not to argue with it.
    """

    @staticmethod
    def _write(repo: Path, relative: str, body: str) -> Path:
        target = repo / relative
        target.write_text(body)
        return target

    def _fire(self, repo: Path, target: Path) -> subprocess.CompletedProcess[str]:
        return run_tool_hook(
            "ruff-on-write.py", "Write", {"file_path": str(target)}, repo, "PostToolUse"
        )

    def test_an_unformatted_write_is_reformatted_in_place(self, python_repo: Path) -> None:
        target = self._write(python_repo, "src/spacing.py", "x = {  'a':1 }\n")
        assert self._fire(python_repo, target).returncode == ALLOW
        assert target.read_text() == 'x = {"a": 1}\n'

    def test_an_auto_fixable_finding_is_fixed_in_place(self, python_repo: Path) -> None:
        """Formatting is not the whole of it: ``ruff check --fix`` is a second pass with
        its own fixes, and import order (``I001``) is the one an agent trips constantly
        and the formatter will never touch."""
        target = self._write(
            python_repo, "src/imports.py", "import os\nimport json\n\nprint(json, os)\n"
        )
        self._fire(python_repo, target)
        assert target.read_text() == "import json\nimport os\n\nprint(json, os)\n"

    def test_the_report_names_the_file_and_the_stale_copy(self, python_repo: Path) -> None:
        """Rewriting a file behind the harness's back invalidates its cached copy, and
        the next ``Edit`` fails with a modification error the agent has no explanation
        for. Saying so is not a courtesy; it is what makes the rewrite survivable."""
        target = self._write(python_repo, "src/spacing.py", "x = {  'a':1 }\n")
        context = context_of(self._fire(python_repo, target))
        assert "src/spacing.py" in context
        assert "stale" in context.lower()

    def test_a_finding_ruff_cannot_fix_is_reported(self, python_repo: Path) -> None:
        """The half a formatter cannot close. ``ci`` would report it minutes later and
        still not fix it, so the agent is the only thing that can, and it needs the
        line."""
        target = self._write(python_repo, "src/undefined.py", "def f():\n    return nope\n")
        context = context_of(self._fire(python_repo, target))
        assert "F821" in context
        assert "src/undefined.py:2:12" in context

    def test_a_clean_file_says_nothing_at_all(self, python_repo: Path) -> None:
        """The common case, and it has to cost nothing. A hook that reports "no changes"
        on every edit spends the context budget it was written to protect."""
        target = self._write(python_repo, "src/clean.py", 'x = {"a": 1}\n')
        result = self._fire(python_repo, target)
        assert result.returncode == ALLOW
        assert result.stdout.strip() == ""

    @pytest.mark.parametrize(
        ("relative", "body"),
        [
            ("notes.md", "# not python\n"),
            ("data.json", '{"a":1}\n'),
        ],
    )
    def test_a_file_ruff_does_not_judge_is_left_alone(
        self, python_repo: Path, relative: str, body: str
    ) -> None:
        target = self._write(python_repo, relative, body)
        result = self._fire(python_repo, target)
        assert result.stdout.strip() == ""
        assert target.read_text() == body

    def test_a_path_outside_the_repo_is_left_alone(self, python_repo: Path, tmp_path: Path) -> None:
        """Scratch space is not this repo's code and is not held to its settings."""
        outside = tmp_path / "elsewhere.py"
        outside.write_text("x = {  'a':1 }\n")
        result = self._fire(python_repo, outside)
        assert result.stdout.strip() == ""
        assert outside.read_text() == "x = {  'a':1 }\n"

    def test_a_clone_with_no_ruff_yet_is_silent(self, python_repo: Path) -> None:
        """A hook runs before ``uv sync`` has necessarily happened. With no binary there
        is nothing to say and nothing to fix, and a complaint would be noise on the one
        turn a new contributor is least able to act on it."""
        (python_repo / ".venv" / "bin" / "ruff").unlink()
        target = self._write(python_repo, "src/spacing.py", "x = {  'a':1 }\n")
        result = self._fire(python_repo, target)
        assert result.returncode == ALLOW
        assert result.stdout.strip() == ""
        assert target.read_text() == "x = {  'a':1 }\n"

    def test_a_write_that_left_no_file_is_not_an_error(self, python_repo: Path) -> None:
        """``tool_input`` names a path; nothing promises it still exists by the time the
        hook runs. Same fail-open rule the guards hold."""
        result = self._fire(python_repo, python_repo / "src" / "vanished.py")
        assert result.returncode == ALLOW
        assert result.stdout.strip() == ""

    def test_another_tool_is_not_this_hooks_business(self, python_repo: Path) -> None:
        """The matcher narrows to ``Edit`` and ``Write``, but a hook that trusts its
        matcher is a hook that misfires the day the matcher is widened."""
        target = self._write(python_repo, "src/spacing.py", "x = {  'a':1 }\n")
        result = run_tool_hook(
            "ruff-on-write.py",
            "Bash",
            {"command": f"touch {target}"},
            python_repo,
            "PostToolUse",
        )
        assert result.stdout.strip() == ""
        assert target.read_text() == "x = {  'a':1 }\n"

    def test_an_unreadable_event_is_not_one_to_act_on(self, python_repo: Path) -> None:
        result = _run_raw("ruff-on-write.py", "not json at all", python_repo)
        assert result.returncode == ALLOW
        assert result.stdout.strip() == ""


class TestResumeFromPlan:
    """Where the work stopped is delivered at session start, not re-derived.

    ``/tdd`` writes the confirmed plan to ``.agents/plans/issue-<N>.md`` precisely so a
    compacted or restarted session resumes without re-exploring. That only works if the
    agent knows to look, and knowing to look was prose in ``CLAUDE.md`` competing with
    everything else in a fresh window. The path is not a judgement call — it is
    ``ralph/issue-<N>`` read off ``git branch`` — so nothing is decided here and nothing
    is refused. The file the session was going to need is simply already in the window.
    """

    def test_the_open_behaviors_arrive_with_the_session(self, ralph_repo: Path) -> None:
        out = run_session_hook("resume-from-plan.py", ralph_repo).stdout
        assert "#900" in out
        assert ".agents/plans/issue-900.md" in out
        assert "a leverage above `max_leverage` is refused" in out
        assert "the engine reads the resolved book" in out
        assert "`CLAUDE.md` gains the bound" in out

    def test_a_ticked_behavior_is_counted_not_printed(self, ralph_repo: Path) -> None:
        """What is done is a number; what is open is the work. Reprinting the finished
        half every session spends the budget this hook exists to save."""
        out = run_session_hook("resume-from-plan.py", ralph_repo).stdout
        assert "a leverage below 1 is refused" not in out
        assert "1 of 4" in out

    def test_the_recorded_shas_are_flagged_as_needing_confirmation(self, ralph_repo: Path) -> None:
        """The plan is written by hand, so it can be ahead of or behind what landed.
        Handing it over without saying so would turn a resume aid into a trusted source."""
        out = run_session_hook("resume-from-plan.py", ralph_repo).stdout
        assert "git log" in out

    def test_a_long_checklist_is_capped(self, ralph_repo: Path) -> None:
        """A 40-item plan dumped whole is the cost this hook was written to avoid."""
        plan = ralph_repo / ".agents" / "plans" / "issue-900.md"
        plan.write_text("".join(f"- [ ] behavior {n}\n" for n in range(40)))
        out = run_session_hook("resume-from-plan.py", ralph_repo).stdout
        assert out.count("- [ ]") < 40
        assert "more" in out

    def test_a_finished_checklist_says_so(self, ralph_repo: Path) -> None:
        """Nothing open is a fact worth stating: it means the slice may be ready to ship,
        which is a different next step from resuming one."""
        plan = ralph_repo / ".agents" / "plans" / "issue-900.md"
        plan.write_text("- [x] 1. done   abc1234\n- [x] 2. also done   def5678\n")
        out = run_session_hook("resume-from-plan.py", ralph_repo).stdout
        assert "#900" in out
        assert "2 of 2" in out

    def test_a_branch_that_names_no_issue_gets_nothing(self, ralph_repo: Path) -> None:
        _git(ralph_repo, "checkout", "-q", "main")
        assert run_session_hook("resume-from-plan.py", ralph_repo).stdout == ""

    def test_an_issue_with_no_plan_yet_gets_nothing(self, ralph_repo: Path) -> None:
        """The first session of a slice, before ``/tdd`` has confirmed anything. There is
        nothing to hand over and an announcement would be noise."""
        (ralph_repo / ".agents" / "plans" / "issue-900.md").unlink()
        assert run_session_hook("resume-from-plan.py", ralph_repo).stdout == ""

    def test_a_directory_that_is_not_a_repo_gets_nothing(self, tmp_path: Path) -> None:
        loose = tmp_path / "loose"
        loose.mkdir()
        assert run_session_hook("resume-from-plan.py", loose).stdout == ""

    def test_an_unreadable_event_is_not_one_to_answer(self, ralph_repo: Path) -> None:
        result = _run_raw("resume-from-plan.py", "", ralph_repo)
        assert result.returncode == ALLOW
        assert result.stdout == ""

    @pytest.mark.parametrize("source", ["startup", "resume", "clear", "compact"])
    def test_every_way_a_session_begins_is_answered(self, ralph_repo: Path, source: str) -> None:
        """No matcher on the wiring, on purpose. A compaction is the moment the plan is
        most needed and the one a ``startup``-only matcher would miss."""
        assert "#900" in run_session_hook("resume-from-plan.py", ralph_repo, source).stdout


class TestWiring:
    """A guard nothing runs is a guard that does not exist.

    Every case above drives a hook directly, which proves the script decides correctly
    and proves nothing at all about whether Claude Code ever calls it. The wiring in the
    committed ``.claude/settings.json`` is the other half, and it is committed rather
    than local precisely so it reaches every clone — the point of the exercise is other
    people's agents, not only the maintainer's.
    """

    @staticmethod
    def _wiring() -> list[tuple[str, str, str]]:
        """Every wired hook as ``(event, matcher, command)``.

        Every event key is walked, not just ``PreToolUse``. A hook filed under the wrong
        event passes every direct-invocation case in this file — the script is fine, it
        is simply never called — and reading one event only would make that invisible.
        """
        settings = json.loads((_HOOKS.parent / "settings.json").read_text())
        return [
            (event, entry.get("matcher", ""), hook["command"])
            for event, entries in settings["hooks"].items()
            for entry in entries
            for hook in entry["hooks"]
        ]

    @pytest.mark.parametrize(
        ("hook", "event", "tools"),
        [
            ("no-tracked-writes.py", "PreToolUse", ["Bash"]),
            ("no-excluded-reads.py", "PreToolUse", ["Bash"]),
            ("no-global-installs.py", "PreToolUse", ["Bash"]),
            ("no-unsliced-doc-reads.py", "PreToolUse", ["Read", "Bash"]),
            ("no-unlinked-prs.py", "PreToolUse", ["Bash"]),
            ("ruff-on-write.py", "PostToolUse", ["Edit", "Write"]),
            ("resume-from-plan.py", "SessionStart", []),
        ],
    )
    def test_every_hook_is_wired_to_the_event_and_tools_it_judges(
        self, hook: str, event: str, tools: list[str]
    ) -> None:
        """The event is half the claim and the tool list is the other half.
        ``no-unsliced-doc-reads`` decides on both a ``Read`` and a ``Bash`` event, and a
        matcher naming only one of them would leave the guard passing every test above
        while the ``cat`` door stayed open in the loop it was written for.

        ``resume-from-plan`` names no tool because ``SessionStart`` has none, and it
        deliberately carries no ``source`` matcher either — asserted below.
        """
        matchers = [
            matcher
            for wired, matcher, command in self._wiring()
            if command.endswith(hook) and wired == event
        ]
        assert matchers, f"{hook} is wired to nothing under {event}"
        for tool in tools:
            assert any(tool in matcher.split("|") for matcher in matchers), (hook, tool)

    def test_the_session_hook_answers_every_way_a_session_begins(self) -> None:
        """A ``source`` matcher would be the one mistake that costs the most: ``compact``
        is when the plan is most needed, and a ``startup``-only wiring misses exactly it.
        No matcher means every source."""
        matchers = [
            matcher
            for event, matcher, command in self._wiring()
            if event == "SessionStart" and command.endswith("resume-from-plan.py")
        ]
        assert matchers == [""], matchers

    def test_every_wired_path_exists_and_runs(self) -> None:
        """``${CLAUDE_PROJECT_DIR}`` is what keeps the wiring correct from a subdirectory
        or a worktree; a relative path would resolve against whatever cwd the call had."""
        for _, _, command in self._wiring():
            assert command.startswith("${CLAUDE_PROJECT_DIR}/")
            path = _HOOKS.parent.parent / command.removeprefix("${CLAUDE_PROJECT_DIR}/")
            assert path.is_file(), command
            assert os.access(path, os.X_OK), command

    def test_the_closes_pattern_is_the_one_ci_holds(self) -> None:
        """``no-unlinked-prs`` front-runs ``pr-policy``'s *Body closes an issue* step, so
        the two have to accept the same bodies. There is no predicate to derive one from
        the other, so this test is what stands in — the same standing as the corpus-glob
        assertion below. A hook stricter than the check it front-runs refuses bodies that
        would have passed, and a false refusal is the failure worth guarding against."""
        source = (_HOOKS / "no-unlinked-prs.py").read_text()
        pattern = source.split('_CLOSES = re.compile(r"', 1)[1].split('"', 1)[0]
        workflow = (_ROOT / ".github" / "workflows" / "pr-policy.yml").read_text()
        assert pattern in workflow, pattern

    def test_every_corpus_glob_matches_a_real_file(self) -> None:
        """``no-unsliced-doc-reads`` is the one guard whose subject is a hand-written
        list rather than a question put to git, so it is the one that can be silently
        disarmed by a rename. Renaming ``docs/module-maps/`` would leave every other test
        in this file green and the maps unguarded; this is what goes red instead."""
        root = _HOOKS.parent.parent
        source = (_HOOKS / "no-unsliced-doc-reads.py").read_text()
        corpus = source.split("_CORPUS = (", 1)[1].split(")", 1)[0]
        globs = [line.strip().strip('",') for line in corpus.splitlines() if '"' in line]

        assert len(globs) == 4, globs
        for glob in globs:
            assert list(root.glob(glob)), glob

    def test_doc_slice_is_where_the_guard_looks_for_it(self) -> None:
        """The guard falls open when the tool is missing, since a refusal with no index
        to offer is an obstacle rather than a guard. That makes a moved ``doc-slice`` a
        silent disarming too."""
        tool = _HOOKS.parent.parent / ".agents" / "tools" / "doc-slice"
        assert tool.is_file()
        assert os.access(tool, os.X_OK)

    def test_ruff_is_where_the_hook_looks_for_it(self) -> None:
        """``ruff-on-write`` hardcodes ``<root>/.venv/bin/ruff`` and falls silent when
        nothing is there, because a fresh clone has not run ``uv sync`` yet. That makes a
        moved venv another silent disarming — so it is asserted rather than assumed.
        This suite runs out of that venv, so its absence is a real failure, not a skip."""
        assert _RUFF.is_file()
        assert os.access(_RUFF, os.X_OK)
