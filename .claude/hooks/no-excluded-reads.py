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

from _shell import command_name, segments, unwrap

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
# same name elsewhere on the filesystem is not covered by it. Named without a trailing
# separator because `_readable` adds one for the sub-path test and compares the bare form
# for the directory itself — the sibling `.agents/plans-old` must not inherit the pass.
_READABLE_IGNORED = (".agents/plans",)

# Readers whose first non-flag argument is a *pattern*, not a path. Offering it as a
# candidate hands `check-ignore` a string that never named a file, and `.env` is a string
# this repo's code and docs name constantly — `grep -rn '.env' src/` opens nothing
# ignored, so refusing it is the misfire that teaches an agent to route around the guard.
_PATTERN_FIRST = frozenset({"grep", "egrep", "fgrep", "rg", "ag", "ack"})

# Once the pattern rides one of these, every non-flag argument left is a path again.
_PATTERN_FLAGS = frozenset({"-e", "--regexp", "-f", "--file"})


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


def _readable(relative: str) -> bool:
    """Whether an ignored path is one of the trees ignored *in order* to be read.

    The directory counts, not only what is under it: sweeping the plans with `grep -rn`
    is how "which plan mentions this behavior" gets asked, and `_repo_relative` runs
    through `normpath`, which drops the trailing separator whether or not it was typed.
    """
    return any(
        relative == entry or relative.startswith(entry + os.sep) for entry in _READABLE_IGNORED
    )


def _read_candidates(segment: list[str]) -> list[str]:
    """The paths this one segment would open, or none at all if it is not a read.

    Still over-collects — a `--include` glob lands here beside the tree it filters — and
    that is affordable, because a candidate only becomes a refusal once `check-ignore`
    matches it. A grep pattern is the one token where it is not: the pattern is written
    to *name* things, so it matches an ignored path by design rather than by accident.
    """
    name = command_name(segment)
    if name not in _READERS:
        return []

    # Only within the grep family: `-f` there names a file of patterns, and to `tail` it
    # means follow and consumes nothing. Reading the flag set command-wide would eat the
    # path out of `tail -f logs/run.log`.
    searching = name in _PATTERN_FIRST

    paths: list[str] = []
    pattern_from_flag = False
    skip_value = False
    for tok in unwrap(segment)[1:]:
        if skip_value:
            skip_value = False
            continue
        if tok.startswith("-"):
            if searching and tok in _PATTERN_FLAGS:
                pattern_from_flag = True
                skip_value = True
            elif searching and tok.split("=", 1)[0] in _PATTERN_FLAGS:
                pattern_from_flag = True  # the `--regexp=x` form carries its own value
            continue
        paths.append(tok)

    if searching and not pattern_from_flag and paths:
        return paths[1:]
    return paths


def _excluded(cwd: str, candidates: list[str]) -> list[str]:
    """The candidates git ignores, minus the ones ignored in order to be read."""
    root = _repo_root(cwd)
    if root is None:
        return []

    found: list[str] = []
    for cand in candidates:
        relative = _repo_relative(root, cwd, cand)
        if relative is None or _readable(relative):
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
    for segment in segments(command):
        candidates.extend(_read_candidates(segment))

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
