#!/usr/bin/env python3
"""SessionStart — hand the current slice's open behaviors to the session that needs them.

`/tdd` writes the confirmed plan to `.agents/plans/issue-<N>.md` precisely so a compacted
or restarted session resumes without re-exploring. That only works if the agent knows to
look, and knowing to look was prose in `CLAUDE.md` competing with everything else in a
fresh window — the failure `no-unsliced-doc-reads` was written for, one layer up.

The path is not a judgement call. It is `ralph/issue-<N>` read off `git branch`, so there
is nothing here to decide and nothing to refuse: the file the session was going to need is
simply already in the window. `SessionStart` stdout becomes context, which is the whole
mechanism.

What is ticked is **counted, not printed**. The finished half is a number; the open half
is the work. Reprinting what is done every session spends the budget this exists to save,
and the same reasoning caps a long list rather than dumping it.

The handover names `git log` on its way out, and that is not politeness. A plan is written
by hand, so it can be ahead of what landed (a behavior ticked before the commit) or behind
it (a commit made without ticking). Handing it over silently would turn a resume aid into
a trusted source, and *confirm, don't trust* is the rule that keeps it useful.

Everything reads locally — no `gh issue view`. A network call at session start is slow on
every session and fails on the offline ones, to add a title the plan already carries.

It answers every `source`, so the wiring carries no matcher: a compaction is the moment
the plan is most needed and exactly the one a `startup`-only matcher would miss.
"""

import json
import os
import re
import subprocess
import sys

_BRANCH = re.compile(r"^ralph/issue-(\d+)$")
_OPEN = re.compile(r"^\s*[-*] \[ \] ")
_TICKED = re.compile(r"^\s*[-*] \[[xX]\] ")

# Enough to see the shape of what is left; past it the plan itself is the right read.
_MAX_OPEN = 12


def _repo_root(cwd: str) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _issue(cwd: str) -> str | None:
    """The issue the current branch names, or None off a `ralph/issue-<N>` branch."""
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd, capture_output=True, text=True
    )
    if result.returncode != 0:
        return None
    match = _BRANCH.match(result.stdout.strip())
    return match.group(1) if match else None


def _handover(relative: str, issue: str, lines: list[str]) -> str:
    ticked = [line for line in lines if _TICKED.match(line)]
    still_open = [line.rstrip() for line in lines if _OPEN.match(line)]
    total = len(ticked) + len(still_open)
    if not total:
        return ""

    parts = [
        f"Resuming issue #{issue}. Its plan is `{relative}` — "
        f"{len(ticked)} of {total} boxes ticked. Read it before exploring anything else."
    ]
    if still_open:
        shown = still_open[:_MAX_OPEN]
        remainder = len(still_open) - len(shown)
        tail = f"\n… and {remainder} more in the file." if remainder else ""
        parts.append("Still open:\n" + "\n".join(shown) + tail)
    else:
        parts.append(
            "Nothing is open. The slice may be ready to ship rather than to resume — "
            "check the PR before starting anything new."
        )
    parts.append(
        "The plan records a sha per ticked behavior. Confirm them against `git log` before "
        "resuming: it is written by hand, so it can be ahead of what landed or behind it."
    )
    return "\n\n".join(parts)


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # An event this hook cannot read is not one it may answer.

    cwd = event.get("cwd") or os.getcwd()
    root = _repo_root(cwd)
    if root is None:
        return 0

    issue = _issue(cwd)
    if issue is None:
        return 0  # Not a slice branch, so there is no plan to be looking for.

    relative = os.path.join(".agents", "plans", f"issue-{issue}.md")
    try:
        with open(os.path.join(root, relative)) as handle:
            lines = handle.readlines()
    except OSError:
        return 0  # The first session of a slice, before `/tdd` has confirmed anything.

    handover = _handover(relative, issue, lines)
    if handover:
        print(handover)
    return 0


if __name__ == "__main__":
    sys.exit(main())
