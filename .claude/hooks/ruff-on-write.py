#!/usr/bin/env python3
"""PostToolUse:Edit|Write — format and fix the Python file that was just written.

`ci` runs `ruff format --check .` and `ruff check .` and **reports only**. Neither
auto-fixes, so a single formatting slip costs a red run, a fix commit and a second run —
minutes, for a change the tool sitting in `.venv` performs in milliseconds. This closes
that window at the edit that caused it.

`PostToolUse` cannot block, and that is the right event rather than a limitation the hook
works around. The write already happened; the useful act is to correct it, not to argue
with it. So unlike the five `PreToolUse` guards this one has no refusal — it fixes the
file and reports what it did.

**It calls `.venv/bin/ruff` directly, never `uv run ruff`.** `uv run` re-syncs the
environment before it hands over, which measures 1.99 s against 0.013 s for the binary. A
two-second tax on every edit is a hook someone turns off, and a hook that is off enforces
nothing.

Three things it deliberately does not do:

- **It does not run mypy.** That was the original sketch and the note is the problem.
  Ruff's half *fixes*, so it spends no context and asks for no judgement. A type check can
  only report, and mid-red a TDD step legitimately type-errors — so the note would be
  noise on exactly the edits this hook fires hardest on, and an agent that learns to skim
  it skims the stale-copy line beside it. `ci` keeps mypy, where a complete diff makes the
  reading trustworthy.
- **It does not touch anything but the one file named in the event.** A repo-wide `--fix`
  would rewrite files the agent never opened, inside a commit about something else.
- **It says nothing when there is nothing to say.** That is the common case, and a hook
  reporting "no changes" on every edit spends the budget it exists to protect.

**`check --fix` runs first and `format` second**, which is ruff's own documented order and
not a preference. A fix applied after formatting is never formatted, and several safe ones
move the lines around them rather than one expression inside them — `UP035` taking the last
`typing` import out leaves the blank lines it stood between behind. The wrong way round, the
hook rewrites a file into a state `ruff format --check .` rejects while reporting only that
it rewrote it: the failure it exists to prevent, delivered with a false all-clear.

That order costs a third call. What `check --fix` prints is written against a file the
formatter then moves, so the findings are re-read afterwards rather than carried over — a
line number is the whole of what an unfixable finding is worth, and one pointing into the
file as it was before this hook rewrote it sends the agent looking.

One consequence worth stating: rewriting a file behind the harness's back invalidates its
cached copy, and the next `Edit` fails with a modification error. The report says so. It
is safe to rewrite the *whole* file rather than only the edited span because `ci` gates
`ruff format --check .` on `main`, so anything found unformatted here was written in this
session.
"""

import json
import os
import subprocess
import sys

# Enough for a cold `ruff` on a large file, short enough that a wedged process cannot
# hold up the loop. Exceeding it is silence, not a complaint: same fail-open rule the
# guards hold.
_TIMEOUT_SECONDS = 20

# Findings are a prompt to act, not a report to read. Past this many the file needs a
# pass of its own, and printing all of them would spend the context the fix needs.
_MAX_FINDINGS = 20


def _repo_root(cwd: str) -> str | None:
    """The repo `cwd` sits in, or None when it is not in one."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _target(event: dict[str, object], cwd: str) -> tuple[str, str] | None:
    """The written file as `(repo root, repo-relative path)`, or None if out of scope.

    Out of scope covers every fail-open case at once: another tool, a path that is not
    Python, a path outside the repo, and a path nothing exists at — `tool_input` names a
    file but nothing promises it survived to the moment this hook runs.
    """
    if event.get("tool_name") not in ("Edit", "Write"):
        return None

    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    path = tool_input.get("file_path")
    if not isinstance(path, str) or not path.endswith(".py"):
        return None

    root = _repo_root(cwd)
    if root is None:
        return None

    absolute = os.path.normpath(os.path.join(cwd, os.path.expanduser(path)))
    if not absolute.startswith(root + os.sep) or not os.path.isfile(absolute):
        return None
    return root, os.path.relpath(absolute, root)


def _ruff(root: str, *args: str) -> subprocess.CompletedProcess[str] | None:
    """Run the project's own ruff from the repo root, or None if it cannot be run.

    The path is resolved from the root of the repo being edited rather than from this
    file, so a worktree or a second clone gets its own. `--force-exclude` is what makes
    the verdict here the same one `ci` reaches: ruff honours `exclude` while walking a
    directory but not for a file named on the command line, and without it this hook
    would reformat a path `ruff format --check .` deliberately skips.
    """
    binary = os.path.join(root, ".venv", "bin", "ruff")
    if not os.access(binary, os.X_OK):
        return None  # A clone that has not run `uv sync` yet has nothing to run.
    try:
        return subprocess.run(
            [binary, *args, "--force-exclude"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _findings(output: str, relative: str) -> list[str]:
    """The `path:line:col: CODE message` lines, dropping ruff's own summary line."""
    return [line for line in output.splitlines() if line.startswith(relative + ":")]


def _report(relative: str, rewritten: bool, findings: list[str]) -> str:
    parts: list[str] = []
    if rewritten:
        parts.append(
            f"ruff rewrote `{relative}` (`check --fix`, then format). Your cached copy of "
            "it is stale — re-read the file before editing it again."
        )
    if findings:
        shown = findings[:_MAX_FINDINGS]
        remainder = len(findings) - len(shown)
        tail = f"\n… and {remainder} more." if remainder else ""
        parts.append(
            "ruff cannot fix these, and `ci` reports them without fixing them either:\n"
            + "\n".join(shown)
            + tail
        )
    return "\n\n".join(parts)


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # An event this hook cannot read is not one it may act on.

    cwd = event.get("cwd") or os.getcwd()
    target = _target(event, cwd)
    if target is None:
        return 0
    root, relative = target

    absolute = os.path.join(root, relative)
    with open(absolute, "rb") as handle:
        before = handle.read()

    if _ruff(root, "check", "--fix", "--output-format=concise", relative) is None:
        return 0
    if _ruff(root, "format", relative) is None:
        return 0

    # Read again rather than keeping the fixing pass's output: the formatter has moved
    # the lines under it, and a finding is only actionable at the line it is now on.
    remaining = _ruff(root, "check", "--output-format=concise", relative)
    if remaining is None:
        return 0

    with open(absolute, "rb") as handle:
        after = handle.read()

    report = _report(relative, after != before, _findings(remaining.stdout, relative))
    if not report:
        return 0

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": report,
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
