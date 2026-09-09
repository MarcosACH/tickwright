"""The ``.claude/hooks`` guards: rules enforced at the tool call, not asked for in prose.

Four ``PreToolUse`` hooks — three on ``Bash``, one on ``Read`` and ``Bash`` both. Each
reads the event JSON on stdin and answers with an exit code — ``0`` allows the call, ``2``
blocks it and hands the text on stderr back to the agent as the reason. That contract is
the whole subject here, so nothing is mocked: the hooks are run as real processes, the way
Claude Code runs them.

Three of the four decide by asking a real tool about the path — ``git`` (tracked?
ignored?) or ``doc-slice`` (what are its sections?) — so the fixtures are real scratch
repos rather than stubbed answers, the same shape and the same reason as
``tests/test_githooks.py``. Global and system git config are pinned to ``/dev/null`` so a
developer's own settings cannot reach an outcome.

The hooks are held to the stdlib alone and to ``/usr/bin/env python3``: they run before
``uv sync`` has necessarily happened on a fresh clone, and the project venv is not
reliably on a hook's PATH.
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

_HOOKS = Path(__file__).resolve().parent.parent / ".claude" / "hooks"


def _load_guard(filename: str) -> ModuleType:
    """Import a hook as a module, for the few assertions that need its own helpers.

    Every other case here drives a hook as a real process, which is what it is — but a
    guard's *inputs* are sometimes checkable only from inside, and scraping a regex out
    of the source text would be a second copy of it. A hook runs as a script, so its own
    directory is `sys.path[0]`; importing one means putting it there by hand.
    """
    if str(_HOOKS) not in sys.path:
        sys.path.insert(0, str(_HOOKS))
    spec = importlib.util.spec_from_file_location(filename.removesuffix(".py"), _HOOKS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    """A scratch repo carrying two tracked files, one ignored tree, and one plan file.

    ``.gitignore`` mirrors the shape of the real one that matters to these hooks: a
    build/venv tree, a log, and ``.agents/plans/`` — which is ignored *and* meant to be
    read, so it is the exception the read guard has to carry.

    **Two** tracked files under ``src/``, because one cannot tell a glob from a single
    path: ``src/*.py`` matching exactly one file is the case that passes by accident.
    """
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / "logs").mkdir()
    (root / ".agents" / "plans").mkdir(parents=True)

    (root / ".gitignore").write_text(".venv/\nlogs/\n*.log\n.agents/plans/\n.env\n")
    (root / "src" / "tracked.py").write_text("x = 1\n")
    (root / "src" / "tracked_too.py").write_text("y = 1\n")
    (root / ".venv" / "bin" / "ruff").write_text("#!/bin/sh\n")
    (root / "logs" / "run.log").write_text("noise\n")
    (root / ".agents" / "plans" / "issue-1.md").write_text("- [ ] behavior\n")
    (root / ".env").write_text("TICKWRIGHT_HYPERLIQUID__SIGNING_KEY=0xdead\n")

    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "add", ".gitignore", "src/tracked.py", "src/tracked_too.py")
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
    # Nested one level below the glob, which is what the depth comparison in `_in_corpus`
    # decides on: `fnmatch`'s `*` crosses `/` happily, so `docs/adr/*.md` would claim this
    # too and an archived ADR would be refused as if it were the live corpus.
    (root / "docs" / "adr" / "archive").mkdir()
    (root / "docs" / "adr" / "archive" / "0001-old.md").write_text(
        "# ADR-0001: A retired decision\n\n## Decision\n\nSuperseded.\n"
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


def run_tool_hook(
    name: str, tool: str, tool_input: dict[str, object], cwd: Path
) -> subprocess.CompletedProcess[str]:
    """Drive one hook with the event Claude Code would hand it."""
    event = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": tool_input,
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


def run_hook(
    name: str, command: str, cwd: Path, tool: str = "Bash"
) -> subprocess.CompletedProcess[str]:
    """Drive one hook with a Bash call — the shape three of the four guards judge."""
    return run_tool_hook(name, tool, {"command": command}, cwd)


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
            "echo 'x = 2' | sudo tee src/tracked.py",
            # A reserved word stands where a program does and, unlike a wrapper, is not a
            # program at all — so a guard keyed on the first token reads `do` and allows
            # the write. The loop is the form an agent reaches for to make one edit across
            # several files, which is the case this guard exists for.
            "for f in a b; do sed -i '' 's/x/y/' src/tracked.py; done",
            "echo 'x = 2' | while read l; do tee src/tracked.py; done",
            "time sed -i '' 's/x/y/' src/tracked.py",
            # `punctuation_chars` groups a run of punctuation into one token, so these
            # arrive whole and equal neither `>` nor `>>`. They truncate the file all the
            # same: `&>` is bash's both-streams form and `>|` overrides noclobber.
            "uv run pytest &> src/tracked.py",
            "uv run pytest &>> src/tracked.py",
            "echo 'x = 2' >| src/tracked.py",
            # `>& file` writes both streams to a file; only `>&<digit>` duplicates a
            # descriptor, and that is what separates this from the `2>&1` below.
            "uv run pytest >& src/tracked.py",
            # A glob is a pathspec git resolves, not a value only the shell knows — it
            # lexes whole and stands in the argument position the guard already reads.
            # Deciding it by how *many* files came back fires backwards, allowing the
            # write in proportion to how many it rewrites, and the multi-file edit is
            # the case this guard exists for.
            "sed -i '' 's/x/y/' src/*.py",
            "echo 'x = 2' | tee src/*.py",
        ],
    )
    def test_a_write_at_a_tracked_path_is_refused(self, repo: Path, command: str) -> None:
        result = run_hook("no-tracked-writes.py", command, repo)
        assert result.returncode == BLOCK
        assert "src/tracked.py" in result.stderr

    def test_a_glob_refusal_names_every_file_it_would_rewrite(self, repo: Path) -> None:
        """The reason is the agent's only account of what the call would have done, and
        one name out of a glob's fifty is the wrong account."""
        result = run_hook("no-tracked-writes.py", "sed -i '' 's/x/y/' src/*.py", repo)
        assert "src/tracked.py" in result.stderr
        assert "src/tracked_too.py" in result.stderr

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
            # `tee` in the *argument* position is a word being searched for, not a program
            # being run — and it is an ordinary word to search this repo for, since the
            # hook, its test and CONTRIBUTING.md all document the `tee` clause.
            "grep -n 'tee' src/tracked.py",
            "rg tee src/tracked.py",
            # The descriptor-duplicating forms name no file: the token after `>&` is a
            # file descriptor, so there is nothing here to stale.
            "uv run pytest >&2",
            "uv run pytest > /tmp/scratch.txt 2>&1",
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
            # Peeling the reserved word exposes the program behind it and nothing else:
            # the loop *list* names a tracked file, and iterating over a file is not
            # writing to it.
            "for f in src/tracked.py; do echo $f; done",
            # A directory is what the cardinality rule was really excluding: git answers
            # a directory pathspec with every file beneath it, and none of them is the
            # write target. Tested directly now, so the glob above can be refused.
            "sed -i '' 's/x/y/' src",
            "echo 'x = 2' | tee src",
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
            # A reserved word is where a program stands without being one. `do`, `then`
            # and `time` each leave the reader one token further along, and a guard that
            # reads only the first token of the segment finds a word it has no rule for.
            "while read l; do cat logs/run.log; done",
            "if grep -q 'noise' logs/run.log; then echo hit; fi",
            "time cat .env",
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
            # The reserved-word peel exposes the reader; it does not widen what counts as
            # one. Both of these read tracked source from inside a construct.
            "while read l; do cat src/tracked.py; done",
            "if grep -q 'x' src/tracked.py; then echo hit; fi",
            # A redirect *target* is where output goes, not a file being read. Sending a
            # run into `logs/` is the ordinary use of an ignored tree — the point of
            # ignoring it — and this guard refusing it says "derive it with a command
            # that reports" about a command that was already reporting.
            "cat src/tracked.py > logs/out.log",
            "grep -n 'x' src/tracked.py >> logs/out.log",
            "cat src/tracked.py &> logs/out.log",
            "cat src/tracked.py 2> logs/err.log",
        ],
    )
    def test_a_read_that_costs_no_context_is_allowed(self, repo: Path, command: str) -> None:
        assert run_hook("no-excluded-reads.py", command, repo).returncode == ALLOW

    def test_a_read_that_also_redirects_is_still_a_read(self, repo: Path) -> None:
        """The other half of dropping redirect targets: the *source* is untouched by it.
        ``cat logs/run.log > /tmp/x`` still spends the file, and a scan that dropped the
        whole tail of the segment rather than the operator and its target would let it
        through."""
        result = run_hook("no-excluded-reads.py", "cat logs/run.log > /tmp/copy", repo)
        assert result.returncode == BLOCK
        assert "logs/run.log" in result.stderr

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
            # And a reserved word is not the program either — the loop and the conditional
            # put the install one token past where a first-token read looks, and this
            # guard is the one whose miss survives the branch, the PR and the revert.
            "if true; then pip install httpx; fi",
            "for p in httpx; do sudo pip install $p; done",
            "nohup pip install httpx",
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
            # The sanctioned command stays sanctioned inside a construct.
            "for p in httpx; do uv add $p; done",
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
        "path",
        ["README.md", "docs/agents/guide.md", ".agents/tools/doc-slice"],
    )
    def test_a_file_outside_the_corpus_is_read_whole(self, docs_repo: Path, path: str) -> None:
        """The corpus is four globs, not "documentation". ``docs/agents/`` is workflow
        prose read end to end on purpose, and a guard that reached it would be charging
        for the cheap files to protect the expensive ones."""
        result = run_tool_hook("no-unsliced-doc-reads.py", "Read", {"file_path": path}, docs_repo)
        assert result.returncode == ALLOW

    @pytest.mark.parametrize(
        "command",
        [
            "Read docs/adr/archive/0001-old.md",
            "cat docs/adr/archive/0001-old.md",
        ],
    )
    def test_a_file_one_level_below_a_corpus_glob_is_read_whole(
        self, docs_repo: Path, command: str
    ) -> None:
        """``fnmatch``'s ``*`` crosses ``/``, so ``docs/adr/*.md`` claims
        ``docs/adr/archive/0001-old.md`` unless the depth is compared too. An archive is
        where a superseded ADR goes precisely so that nobody plans against it, and
        charging the slicing toll on one would be the guard reaching past its corpus.

        Both doors, because the depth test lives in ``_in_corpus``, which is behind both
        and would be deleted once for both."""
        tool, path = command.split(" ", 1)
        if tool == "Read":
            result = run_tool_hook(
                "no-unsliced-doc-reads.py", "Read", {"file_path": path}, docs_repo
            )
        else:
            result = run_hook("no-unsliced-doc-reads.py", command, docs_repo)
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

    @pytest.mark.parametrize(
        "command",
        [
            "sudo cat docs/adr/0001-a-decision.md",
            "for f in a b; do cat docs/adr/0001-a-decision.md; done",
            "PAGER=cat cat docs/adr/0001-a-decision.md",
        ],
    )
    def test_a_dumper_behind_a_wrapper_or_a_keyword_is_still_a_dumper(
        self, docs_repo: Path, command: str
    ) -> None:
        """The bypass the other three guards close through ``_shell.unwrap``, asserted
        here too because this guard is the fourth door onto the same corpus. Key on the
        raw first token and ``sudo``, a loop's ``do`` or an assignment prefix stands where
        ``cat`` does: the guard matches nothing and the whole file is spent anyway."""
        result = run_hook("no-unsliced-doc-reads.py", command, docs_repo)
        assert result.returncode == BLOCK

    @pytest.mark.parametrize(
        "command",
        [
            "cat README.md > docs/adr/0001-a-decision.md",
            "cat README.md >> CONTEXT.md",
            "cat README.md &> docs/module-maps/surface.md",
            "cat README.md 2> docs/research/note.md",
        ],
    )
    def test_a_redirect_target_is_not_a_read_of_it(self, docs_repo: Path, command: str) -> None:
        """A corpus file being written to is not one being read, and this guard's whole
        subject is what a call pulls into the window. Refusing here would answer a write
        with "read it by section instead", which is not an instruction that applies —
        and ``no-tracked-writes`` is the guard that has something true to say about it.

        The last two are the shapes a hand-rolled scan misses: ``&>`` arrives as one
        token that equals neither ``>`` nor ``>>``, and ``2>`` lexes as ``2`` then ``>``,
        leaving a bare descriptor standing where a path would."""
        result = run_hook("no-unsliced-doc-reads.py", command, docs_repo)
        assert result.returncode == ALLOW

    def test_a_refused_dump_gets_the_same_index_a_refused_read_does(self, docs_repo: Path) -> None:
        result = run_hook("no-unsliced-doc-reads.py", "cat docs/adr/0001-a-decision.md", docs_repo)
        assert "Consequences" in result.stderr
        assert "(+1)" in result.stderr

    def test_the_hook_is_executable(self) -> None:
        assert os.access(_HOOKS / "no-unsliced-doc-reads.py", os.X_OK)


class TestWiring:
    """A guard nothing runs is a guard that does not exist.

    Every case above drives a hook directly, which proves the script decides correctly
    and proves nothing at all about whether Claude Code ever calls it. The wiring in the
    committed ``.claude/settings.json`` is the other half, and it is committed rather
    than local precisely so it reaches every clone — the point of the exercise is other
    people's agents, not only the maintainer's.
    """

    @staticmethod
    def _wiring() -> list[tuple[str, str]]:
        """Every ``PreToolUse`` hook as ``(matcher, command)``."""
        settings = json.loads((_HOOKS.parent / "settings.json").read_text())
        return [
            (entry.get("matcher", ""), hook["command"])
            for entry in settings["hooks"]["PreToolUse"]
            for hook in entry["hooks"]
        ]

    @pytest.mark.parametrize(
        ("hook", "tools"),
        [
            ("no-tracked-writes.py", ["Bash"]),
            ("no-excluded-reads.py", ["Bash"]),
            ("no-global-installs.py", ["Bash"]),
            ("no-unsliced-doc-reads.py", ["Read", "Bash"]),
        ],
    )
    def test_every_guard_is_wired_to_every_tool_it_judges(
        self, hook: str, tools: list[str]
    ) -> None:
        """The tool list is the second half of the claim. ``no-unsliced-doc-reads``
        decides on both a ``Read`` and a ``Bash`` event, and a matcher naming only one of
        them would leave the guard passing every test above while the ``cat`` door stayed
        open in the loop it was written for."""
        matchers = [matcher for matcher, command in self._wiring() if command.endswith(hook)]
        assert matchers, f"{hook} is wired to nothing"
        for tool in tools:
            assert any(tool in matcher.split("|") for matcher in matchers), (hook, tool)

    def test_every_wired_path_exists_and_runs(self) -> None:
        """``${CLAUDE_PROJECT_DIR}`` is what keeps the wiring correct from a subdirectory
        or a worktree; a relative path would resolve against whatever cwd the call had."""
        for _, command in self._wiring():
            assert command.startswith("${CLAUDE_PROJECT_DIR}/")
            path = _HOOKS.parent.parent / command.removeprefix("${CLAUDE_PROJECT_DIR}/")
            assert path.is_file(), command
            assert os.access(path, os.X_OK), command

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

    def test_the_glossary_still_matches_the_shape_its_index_is_built_from(self) -> None:
        """The third way this guard can be disarmed from outside its own file.

        ``CONTEXT.md`` is the one corpus file indexed by term rather than by heading, and
        ``_refusal_for`` **allows** the whole read when that index comes back empty — a
        refusal with nothing to offer is an obstacle, not a guard. So reformatting the
        glossary to ``**Term** — …`` would leave the largest file in the corpus unguarded
        with every other test here green.

        The count is read from ``CLAUDE.md`` rather than written down twice: the prose
        there quotes it, and a bare ``> 0`` would let the index shrink silently while the
        claim went stale. One number, one place, and this is what compares them.
        """
        root = _HOOKS.parent.parent
        guard = _load_guard("no-unsliced-doc-reads.py")

        index = guard._term_index(str(root / "CONTEXT.md"))
        assert index is not None, "CONTEXT.md yields no terms; the guard now allows it whole"

        claimed = re.search(r"its (\d+) terms", (root / "CLAUDE.md").read_text())
        assert claimed is not None, "CLAUDE.md no longer states the term count"
        assert len(index.splitlines()) == int(claimed.group(1))

    def test_doc_slice_is_where_the_guard_looks_for_it(self) -> None:
        """The guard falls open when the tool is missing, since a refusal with no index
        to offer is an obstacle rather than a guard. That makes a moved ``doc-slice`` a
        silent disarming too."""
        tool = _HOOKS.parent.parent / ".agents" / "tools" / "doc-slice"
        assert tool.is_file()
        assert os.access(tool, os.X_OK)
