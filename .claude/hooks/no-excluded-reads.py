#!/usr/bin/env python3
"""PreToolUse:Bash — refuse a shell read of a path git ignores.

`.claude/settings.json` denies `.venv/`, the caches, `logs/` and the rest to the `Read`
tool. That deny list binds one tool, and `cat`, `head` and `grep` fetch the same bytes
through Bash — which is the door bypass-permissions mode actively pushes an agent
toward. `evals/tdd/plans-with-sliced-reading` names the same evasion in its own grader
comments. This closes it.

**Membership is `git check-ignore`, not a second path list.** `.gitignore` already names
every tree this repo keeps out of a context window, plus `.env`, whose exclusion matters
for a stronger reason than budget. Copying those globs here would be two lists that must
agree, which this project calls a bug — and the derived form also covers whatever gets
ignored next, with nothing to keep in sync.

Two exemptions, both structural rather than taste:

- `.agents/plans/` is ignored *on purpose* and reading it is the entire point of the
  plan-file convention (`CLAUDE.md`, the `/tdd` pipeline). Ignored does not imply
  unreadable, and this is where the two come apart.
- The **executable position**. `.venv/bin/ruff` is ignored and running it is correct —
  calling the venv binaries directly rather than through `uv run` is a 150× difference
  in hook latency. A program being run is not a file being read.
"""

import json
import os
import subprocess
import sys

from _shell import arguments, command_name, segments, tokens

# Commands whose arguments are files they pull into the context window. Narrow on
# purpose: the guard acts only where it is sure a read is what is being asked for, so an
# unrecognised program's arguments are never candidates.
_READERS = frozenset(
    {
        "cat",
        "bat",
        "head",
        "tail",
        "less",
        "more",
        "nl",
        "od",
        "xxd",
        "strings",
        "wc",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "ag",
        "ack",
    }
)

# Ignored and meant to be read. Matched on the repo-relative path, so a directory of the
# same name elsewhere on the filesystem is not covered by it.
_READABLE_IGNORED = (".agents/plans/",)


def _repo_root(cwd: str) -> str | None:
    """The repo `cwd` sits in, or None when it is not in one — asked once per event."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _repo_relative(root: str, cwd: str, path: str) -> str | None:
    """`path` as git would name it, or None when it is outside the repo entirely."""
    absolute = os.path.normpath(os.path.join(cwd, os.path.expanduser(path)))
    if not absolute.startswith(root + os.sep):
        return None
    return os.path.relpath(absolute, root)


def _excluded(cwd: str, candidates: list[str]) -> list[str]:
    """The candidates git ignores, minus the ones ignored in order to be read."""
    root = _repo_root(cwd)
    if root is None:
        return []

    found: list[str] = []
    for cand in candidates:
        relative = _repo_relative(root, cwd, cand)
        if relative is None or relative.startswith(_READABLE_IGNORED):
            continue
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", "--", cand],
            cwd=cwd,
            capture_output=True,
        )
        if ignored.returncode == 0 and relative not in found:
            found.append(relative)
    return found


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # An event this guard cannot read is not one it may block on.

    if event.get("tool_name") != "Bash":
        return 0

    command = event.get("tool_input", {}).get("command", "")
    cwd = event.get("cwd") or os.getcwd()

    candidates: list[str] = []
    for segment in segments(tokens(command)):
        if command_name(segment) in _READERS:
            candidates.extend(arguments(segment))

    excluded = _excluded(cwd, candidates)
    if not excluded:
        return 0

    print(
        f"Blocked: {', '.join(excluded)} is excluded by .gitignore, so it is not part of "
        "this repo's context budget.\n"
        "If you need a fact from a build artefact, a cache or a log, derive it with a "
        "command that reports rather than one that dumps the file.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
