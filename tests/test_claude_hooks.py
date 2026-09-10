"""The ``.claude/hooks`` scripts: work done at the tool call, not asked for in prose.

Seven hooks across three events, and they come in two shapes.

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

_ROOT = Path(__file__).resolve().parent.parent
_HOOKS = _ROOT / ".claude" / "hooks"
_RUFF = _ROOT / ".venv" / "bin" / "ruff"


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

    def test_a_redirect_standing_before_the_program_does_not_hide_it(self, repo: Path) -> None:
        """The `sed -i` arm keys on the program, so a redirect written before it stands
        where `sudo` and a loop's `do` stand and the edit goes through unseen. The
        *redirect* arm never had the gap: `_write_targets` scans the whole segment, so a
        leading `> src/tracked.py` was always caught wherever it sat."""
        result = run_hook(
            "no-tracked-writes.py", "2>/dev/null sed -i '' 's/x/y/' src/tracked.py", repo
        )
        assert result.returncode == BLOCK

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
            # A grep pattern spelled like a redirect. `shlex` strips the quotes, so `'>'`
            # arrives as the operator token itself and is indistinguishable from one —
            # which is why the pattern is taken out of the way *before* the redirect scan
            # runs, rather than after. Filtered first, the log behind it reads as a write
            # target and the guard opens a hole in the commonest reader it covers.
            "grep '>' logs/run.log",
            "grep '>>' logs/run.log",
            "grep -e '2>' logs/run.log",
            # An *input* redirect names a file the command reads, so only the operator is
            # dropped and the operand stays a candidate — `cat < logs/run.log` spends the
            # log exactly as `cat logs/run.log` does. The descriptor prefix goes with it:
            # `0<` lexes as `0` then `<`.
            "cat < logs/run.log",
            "cat 0< logs/run.log",
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
            # A here-string's operand is the data itself. It never named a file, so it
            # goes with its operator rather than being offered as a path that happens to
            # spell one.
            "cat <<< 'logs/run.log'",
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

    @pytest.mark.parametrize(
        "command",
        [
            "2>/dev/null cat logs/run.log",
            "> /tmp/copy cat .env",
            "&> /tmp/copy grep -n 'noise' logs/run.log",
        ],
    )
    def test_a_redirect_standing_before_the_program_does_not_hide_it(
        self, repo: Path, command: str
    ) -> None:
        """A redirect may be written *before* the command it belongs to, and there it
        occupies the executable position exactly as ``sudo`` and a loop's ``do`` do —
        the wrapper problem reached through the grammar again. Read the program off the
        raw first token and ``2>/dev/null cat .env`` names the bare ``2``, matches no
        rule, and hands over the key anyway.

        Peeling is **leading-only**, and that is what keeps it safe here where the
        blanket scan is not: ``shlex`` strips quotes, so the ``'>'`` of
        ``grep '>' logs/run.log`` is indistinguishable from the operator — but a pattern
        is an *argument*, and nothing standing before the program is ever data."""
        assert run_hook("no-excluded-reads.py", command, repo).returncode == BLOCK

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

    def test_a_redirect_standing_before_the_program_does_not_hide_it(self, repo: Path) -> None:
        """The most costly place for this guard to read the wrong token: it fails open,
        so `2>/dev/null pip install httpx` names the bare `2`, matches no rule, and the
        install lands in whatever environment was active."""
        assert (
            run_hook("no-global-installs.py", "2>/dev/null pip install httpx", repo).returncode
            == BLOCK
        )

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
        same reasoning that makes every guard here fail open on input it cannot parse.
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
            "2>/dev/null cat docs/adr/0001-a-decision.md",
            "> /tmp/dump.log cat CONTEXT.md",
            "&> /tmp/dump.log cat docs/module-maps/surface.md",
        ],
    )
    def test_a_redirect_standing_before_the_program_does_not_hide_it(
        self, docs_repo: Path, command: str
    ) -> None:
        """A redirect may precede the command it belongs to, and there it stands exactly
        where ``sudo`` and a loop's ``do`` stand in the case above. Key on the raw first
        token and the program reads as ``>``, or as the bare ``2`` of ``2>``, matching
        nothing while the whole file is spent anyway.

        So the redirect scan comes off before the program is *named*, not merely before
        its paths are collected — one strip feeding both reads."""
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

    @pytest.mark.parametrize("command", ["cat docs/adr/*.md", "cat *.md"])
    def test_a_glob_that_expands_onto_the_corpus_is_refused(
        self, docs_repo: Path, command: str
    ) -> None:
        """The cheapest whole read to write and the most expensive to serve: on the real
        repo ``cat docs/adr/*.md`` is fifty ADRs at once, the single largest spend the
        corpus allows.

        ``shlex`` does not expand globs, so the literal reaches ``os.path.isfile``, which
        says no, and the call goes through. ``no-excluded-reads`` refuses the same shape
        for free because ``git check-ignore`` resolves a pathspec; this guard has no git
        predicate to ask, so it expands the pattern itself."""
        result = run_hook("no-unsliced-doc-reads.py", command, docs_repo)
        assert result.returncode == BLOCK

    def test_a_glob_refusal_names_the_corpus_files_instead_of_indexing_each(
        self, docs_repo: Path
    ) -> None:
        """One index is the answer to a whole read; several are a bigger one than the
        read they refused. ADR-0040's table of contents alone is 1,292 characters, so
        fifty of them cost more than the file the guard was protecting.

        Past one file the refusal therefore names them and asks for a choice. The
        glossary is in this expansion too, which is why the count rather than the
        corpus-file kind is what decides."""
        result = run_hook("no-unsliced-doc-reads.py", "cat *.md docs/adr/*.md", docs_repo)
        assert result.returncode == BLOCK
        assert "CONTEXT.md" in result.stderr
        assert "docs/adr/0001-a-decision.md" in result.stderr
        assert "Consequences" not in result.stderr  # no table of contents was printed

    @pytest.mark.parametrize(
        "command",
        [
            # Expands only onto the archived ADR, which the depth check in `_in_corpus`
            # keeps out of the corpus — the expansion must not smuggle it back in.
            "cat docs/adr/archive/*.md",
            # Expands onto nothing at all, and a pattern naming no file reads none.
            "cat docs/adr/*.rst",
        ],
    )
    def test_a_glob_that_misses_the_corpus_is_allowed(self, docs_repo: Path, command: str) -> None:
        result = run_hook("no-unsliced-doc-reads.py", command, docs_repo)
        assert result.returncode == ALLOW

    @pytest.mark.parametrize("pattern", ["docs/adr/*.md", "docs/adr/0001-a-decision.md"])
    def test_a_home_relative_path_is_judged_whether_or_not_it_globs(
        self, docs_repo: Path, monkeypatch: pytest.MonkeyPatch, pattern: str
    ) -> None:
        """``~`` is expanded by the shell, and by ``os.path.expanduser`` — but not by
        ``glob``, which passes an unexpanded ``~`` straight through and matches nothing.

        The guard expanded the user prefix only when deciding whether a *resolved* path
        was in the corpus, so the same file was refused when named directly and allowed
        the moment a wildcard was added: the empty match fell back to the literal, which
        then failed ``isfile`` because it still held the ``*``. Both spellings are the
        largest read the corpus allows, so both are the guard's subject.

        ``HOME`` is patched into the environment the hook subprocess is handed, since
        that is what ``expanduser`` reads and what makes ``~`` name the scratch repo.
        """
        monkeypatch.setitem(_ENV, "HOME", str(docs_repo.parent))
        command = f"cat ~/{docs_repo.name}/{pattern}"
        result = run_hook("no-unsliced-doc-reads.py", command, docs_repo)
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
            'gh pr create -b="no reference"',
            'gh pr create -b"no reference"',
            'gh -R MarcosACH/tickwright pr create --assignee @me --body "no reference"',
            'git push -u origin HEAD\ngh pr create --title "t" --body "no reference"',
        ],
    )
    def test_every_shape_the_body_arrives_in_is_read(self, ralph_repo: Path, command: str) -> None:
        """The short flag, the ``=`` form, a flag ahead of the subcommand, and the
        multi-line script — the last of which is the regression #307 found live, where
        the lexer discards newlines and a whole script reads as one segment.

        ``-b=…`` and ``-b…`` are the two shorthand spellings ``pflag`` accepts beside the
        separated one, so ``gh`` reads all three as one flag with one value. A guard that
        reads only some of them is not fail-open on an ambiguity — it is blind to a body
        that is fully visible, which is the shape ``CONTRIBUTING.md`` rules out.
        """
        assert run_hook("no-unlinked-prs.py", command, ralph_repo).returncode == BLOCK

    def test_a_body_file_is_read_and_judged(self, ralph_repo: Path) -> None:
        (ralph_repo / "body.md").write_text("It does the thing.\n")
        result = run_hook("no-unlinked-prs.py", "gh pr create -F body.md", ralph_repo)
        assert result.returncode == BLOCK
        assert "Closes #900" in result.stderr

    @pytest.mark.parametrize(
        "command",
        [
            "gh pr create --body-file=body.md",
            "gh pr create -F=body.md",
            "gh pr create -Fbody.md",
        ],
    )
    def test_a_body_file_is_read_in_every_spelling_too(
        self, ralph_repo: Path, command: str
    ) -> None:
        """The ``=`` and attached forms are not a long-flag privilege. ``--body`` handled
        them and ``--body-file`` did not, so the same body went unjudged purely for being
        passed by path — an asymmetry inside one function rather than a decision."""
        (ralph_repo / "body.md").write_text("It does the thing.\n")
        result = run_hook("no-unlinked-prs.py", command, ralph_repo)
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
            "gh pr create --body-file=-",
            "gh pr create -F=-",
            "gh pr create -F-",
            "gh pr create -F missing.md",
            "gh pr create --body-file=missing.md",
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

    @pytest.mark.parametrize(
        "command",
        [
            'gh pr create --title "t" --body "$(cat body.md)"',
            'gh pr create --title "t" --body "`cat body.md`"',
            'gh pr create --title "t" --body "$BODY"',
            'gh pr create --title "t" --body "${BODY}"',
            'gh pr create --title "t" --body "$1"',
        ],
    )
    def test_a_body_the_shell_has_yet_to_expand_is_not_one_this_hook_may_refuse(
        self, ralph_repo: Path, command: str
    ) -> None:
        """A ``--body`` argument is not always its own value.

        ``shlex`` does not expand, so ``"$(cat body.md)"`` arrives as those literal
        characters. Judging them refuses a PR whose real body closes its issue —
        a **false refusal**, which this project calls worse than no guard at all. The body
        is not badly spelled here, it is genuinely hidden, in the same way ``-F -``'s is.

        The file exists and closes #900 in every case below, which is the point: the
        refusal would be wrong on the merits and not merely unlucky.
        """
        (ralph_repo / "body.md").write_text("It does the thing.\n\nCloses #900\n")
        assert run_hook("no-unlinked-prs.py", command, ralph_repo).returncode == ALLOW

    @pytest.mark.parametrize(
        ("body", "expected"),
        [("It does the thing.", BLOCK), ("It does the thing.\n\nCloses #900", ALLOW)],
    )
    def test_a_heredoc_body_is_visible_and_so_is_still_judged(
        self, ralph_repo: Path, body: str, expected: int
    ) -> None:
        """The licence above covers a body that is *hidden*, and this one is not.

        ``--body "$(cat <<'EOF' … EOF)"`` is how this project writes a PR body, and the
        text sits right there in the token: the substitution is opaque, the heredoc it
        feeds is not. Waiving it along with the rest would give the hook away on the one
        form the loop actually reaches for.
        """
        command = f'gh pr create --title "t" --body "$(cat <<\'EOF\'\n{body}\nEOF\n)"'
        assert run_hook("no-unlinked-prs.py", command, ralph_repo).returncode == expected

    @pytest.mark.parametrize(
        ("tail", "expected"),
        [("Closes #900", ALLOW), ("No reference anywhere.", BLOCK)],
    )
    def test_a_terminator_the_shell_would_not_honour_does_not_end_the_body(
        self, ralph_repo: Path, tail: str, expected: int
    ) -> None:
        """A heredoc ends at a terminator in **column 0**, and nowhere else.

        The shell honours a closing line only unindented — leading tabs, and only under
        ``<<-``. So an ``EOF`` inside an indented snippet ends nothing, and reading it as
        a terminator truncates the body there: a ``Closes #N`` standing after the snippet
        is dropped and the PR is refused on a body that closes its issue. That is the
        false refusal the expansion licence above exists to avoid, reached from inside
        the one shape the licence deliberately does not cover.

        The second arm is what keeps the fix from being a waiver: a body carrying an
        inner terminator and no reference at all is still refused.
        """
        body = (
            "Quoting the fixture it writes:\n\n"
            "    cat <<EOF > notes.txt\n    hello\n    EOF\n\n"
            f"{tail}"
        )
        command = f'gh pr create --title "t" --body "$(cat <<\'EOF\'\n{body}\nEOF\n)"'
        assert run_hook("no-unlinked-prs.py", command, ralph_repo).returncode == expected

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

    def test_a_fix_that_moves_the_lines_around_it_is_still_formatted(
        self, python_repo: Path
    ) -> None:
        """The linter's fixes run **before** the formatter, never after it.

        A fix applied after formatting is never formatted. Several safe ones change the
        shape of the file rather than one expression inside it — ``UP035`` taking the last
        ``typing`` import out leaves behind the blank lines it stood between — so the wrong
        order rewrites a file into a state ``ruff format --check .`` rejects, and reports
        only that it rewrote it. That is worse than doing nothing: the hook introduces the
        failure it exists to prevent, and its report is a false all-clear on the one fact
        the agent relies on it for.

        ``I001`` above cannot catch this. Its fix happens to land already formatted, which
        is what let the order look right for as long as it did.
        """
        target = self._write(
            python_repo,
            "src/annotated.py",
            'from typing import List\n\n\ndef g(x: "List[int]") -> int:\n    return len(x)\n',
        )
        self._fire(python_repo, target)

        verdict = subprocess.run(
            [str(_RUFF), "format", "--check", "src/annotated.py", "--force-exclude"],
            cwd=python_repo,
            capture_output=True,
            text=True,
        )
        assert verdict.returncode == 0, verdict.stdout + verdict.stderr

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

    def test_a_path_ruff_itself_excludes_is_left_alone(self, python_repo: Path) -> None:
        """``--force-exclude`` is what makes this hook's verdict the one ``ci`` reaches,
        and it is the one property of the call that nothing else would catch. Ruff honours
        ``exclude`` while walking a directory but **not** for a file named on the command
        line, so without the flag an edit under ``.venv/`` is rewritten inside a commit
        ``ruff format --check .`` never inspects. Dropping it leaves every other case in
        this class green — the silent disarming ``test_every_corpus_glob_matches_a_real_file``
        exists to prevent, one hook over."""
        vendored = python_repo / ".venv" / "lib" / "vendored.py"
        vendored.parent.mkdir(parents=True)
        vendored.write_text("x = {  'a':1 }\n")

        result = self._fire(python_repo, vendored)
        assert result.returncode == ALLOW
        assert result.stdout.strip() == ""
        assert vendored.read_text() == "x = {  'a':1 }\n"

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

    def test_ruff_is_where_the_hook_looks_for_it(self) -> None:
        """``ruff-on-write`` hardcodes ``<root>/.venv/bin/ruff`` and falls silent when
        nothing is there, because a fresh clone has not run ``uv sync`` yet. That makes a
        moved venv another silent disarming — so it is asserted rather than assumed.
        This suite runs out of that venv, so its absence is a real failure, not a skip."""
        assert _RUFF.is_file()
        assert os.access(_RUFF, os.X_OK)
