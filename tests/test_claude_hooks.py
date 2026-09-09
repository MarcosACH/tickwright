"""The ``.claude/hooks`` guards: rules enforced at the tool call, not asked for in prose.

Three ``PreToolUse`` hooks on ``Bash``. Each reads the event JSON on stdin and answers
with an exit code — ``0`` allows the call, ``2`` blocks it and hands the text on stderr
back to the agent as the reason. That contract is the whole subject here, so nothing is
mocked: the hooks are run as real processes, the way Claude Code runs them.

Two of the three decide by asking ``git`` about the path (tracked? ignored?), so the
fixture is a real scratch repo rather than a stubbed answer — the same shape and the same
reason as ``tests/test_githooks.py``. Global and system git config are pinned to
``/dev/null`` so a developer's own settings cannot reach an outcome.

The hooks are held to the stdlib alone and to ``/usr/bin/env python3``: they run before
``uv sync`` has necessarily happened on a fresh clone, and the project venv is not
reliably on a hook's PATH.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

_HOOKS = Path(__file__).resolve().parent.parent / ".claude" / "hooks"

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


def run_hook(
    name: str, command: str, cwd: Path, tool: str = "Bash"
) -> subprocess.CompletedProcess[str]:
    """Drive one hook with the event Claude Code would hand it."""
    event = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": {"command": command},
        "cwd": str(cwd),
    }
    return subprocess.run(
        [str(_HOOKS / name)],
        input=json.dumps(event),
        cwd=cwd,
        env=_ENV,
        capture_output=True,
        text=True,
    )


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
            # A wrapper is not the program. `sudo` in front changes who writes the file,
            # not whether the harness's copy of it goes stale.
            "sudo sed -i '' 's/x/y/' src/tracked.py",
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
            # `sed` without `-i` writes to stdout, and the `-i` belongs to the `grep`
            # upstream of the pipe. Reading the predicate over the whole command sees a
            # `sed` and an `-i` and refuses a command that writes nothing.
            "grep -i 'x' src/tracked.py | sed 's/a/b/'",
            "grep -i 'x' src/tracked.py\nsed 's/a/b/' /tmp/scratch.txt",
            # A heredoc *body* is data, not commands. Writing a new file whose content
            # quotes a shell example must not be read as running that example — the
            # refusal would name a file the command never opens, and the natural cases
            # are this repo's own: a doc, or a test whose fixtures are shell commands.
            "cat <<'DOC' > /tmp/notes.md\necho hi > src/tracked.py\nDOC",
            "cat <<'DOC' > src/brand_new.md\nsed -i '' 's/a/b/' src/tracked.py\nDOC",
            # A newline *inside a quoted argument* is data as well. A multi-line commit
            # message or a `--body` that quotes a shell example is one command, and only
            # the newlines outside the quotes end anything. The example has to sit on an
            # interior line to be worth asserting: the opening and closing lines carry an
            # unbalanced quote, so the lexer already refuses them and the guard fails open
            # for the wrong reason.
            'git commit -m "fix: the write guard\n\necho x > src/tracked.py\n\nis allowed now"',
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
            "sudo cat .env",
            # The pattern came from `-e`, so every non-flag argument left is a path.
            "grep -e 'noise' logs/run.log",
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
            # The directory is the exemption too — "which plan mentions this behavior"
            # is asked by sweeping it, and `normpath` strips the separator a prefix
            # match on `.agents/plans/` needs.
            "grep -rn 'behavior' .agents/plans",
            "grep -rn 'behavior' .agents/plans/",
            # Repo source is the normal case and must stay cheap.
            "cat src/tracked.py",
            "grep -rn 'x' src/",
            # A grep *pattern* is not a path. `.env` and `logs/run.log` are strings this
            # repo's code and docs name constantly, and searching tracked source for one
            # opens nothing ignored — `check-ignore` answers on the string alone.
            "grep -rn '.env' src/",
            "grep -rn 'logs/run.log' src/tracked.py",
            # A newline inside a quoted argument does not end a command, so a message
            # whose second line opens with a reader's name is prose, not a read.
            'git commit -m "docs: note the guard\n\ncat logs/run.log\n\nis how it surfaced"',
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
            # A wrapper is not the program, and this is the form that does the most
            # damage: root, into the system Python. Every branch above is one `sudo`
            # away from doing nothing at all.
            "sudo pip install httpx",
            "sudo -H pip3 install httpx",
            "sudo -u root pip install httpx",
            "sudo python3 -m pip install httpx",
            "sudo npm install -g typescript",
            "sudo brew install jq",
            "env PIP_NO_INPUT=1 pip install httpx",
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
            # Not an install at all — `--system` scopes a query here, and every other
            # branch in the guard gates on the verb.
            "pip --version",
            "brew list",
            "uv pip list --system",
            # A wrapper with no install behind it is not the subject either.
            "sudo -v",
            "sudo launchctl list",
            # A newline inside a quoted argument does not end a command, so writing
            # *about* an install is not performing one.
            'git commit -m "docs: the guard\n\npip install httpx\n\nis what it refuses"',
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


class TestWiring:
    """A guard nothing runs is a guard that does not exist.

    Every case above drives a hook directly, which proves the script decides correctly
    and proves nothing at all about whether Claude Code ever calls it. The wiring in the
    committed ``.claude/settings.json`` is the other half, and it is committed rather
    than local precisely so it reaches every clone — the point of the exercise is other
    people's agents, not only the maintainer's.
    """

    @staticmethod
    def _bash_hook_commands() -> list[str]:
        settings = json.loads((_HOOKS.parent / "settings.json").read_text())
        return [
            hook["command"]
            for entry in settings["hooks"]["PreToolUse"]
            if entry.get("matcher") == "Bash"
            for hook in entry["hooks"]
        ]

    @pytest.mark.parametrize(
        "hook", ["no-tracked-writes.py", "no-excluded-reads.py", "no-global-installs.py"]
    )
    def test_every_guard_is_wired_to_pretooluse_bash(self, hook: str) -> None:
        assert any(command.endswith(hook) for command in self._bash_hook_commands())

    def test_every_wired_path_exists_and_runs(self) -> None:
        """``${CLAUDE_PROJECT_DIR}`` is what keeps the wiring correct from a subdirectory
        or a worktree; a relative path would resolve against whatever cwd the call had."""
        for command in self._bash_hook_commands():
            assert command.startswith("${CLAUDE_PROJECT_DIR}/")
            path = _HOOKS.parent.parent / command.removeprefix("${CLAUDE_PROJECT_DIR}/")
            assert path.is_file(), command
            assert os.access(path, os.X_OK), command
