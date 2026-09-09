#!/usr/bin/env python3
"""PreToolUse:Bash — refuse a `gh pr create` whose body closes no issue.

`.github/workflows/pr-policy.yml` already gates this and stays the floor. It just reports
late: by the time the *Body closes an issue* step runs the PR exists, the check is red,
and clearing it costs a `gh pr edit` and a re-run. Nothing is wrong with the check — the
cost is entirely in *when* it speaks.

**The refusal answers.** The branch is `ralph/issue-<N>`, so the missing line is derivable
rather than merely detectable, and the block reason hands back `Closes #<N>` verbatim.
That matters for the same reason it did in `no-unsliced-doc-reads`: the rule loses on
economics if complying costs a lookup and ignoring it costs nothing.

`_CLOSES` is `pr-policy`'s own pattern rather than this project's narrower `Closes #N`
house style, and a test asserts the two texts still agree. A guard stricter than the check
it front-runs refuses bodies that would have passed — a false refusal, which is the one
failure this project keeps naming as worse than no guard at all.

It reads only the bodies that are actually visible. `--fill`, `--fill-verbose`, `--web`, a
bare invocation that opens an editor, and `-F -` all build the body somewhere this hook
cannot see, so they are allowed. Judging what is not there is guessing.

Two limits worth stating rather than working around:

- `gh pr edit --body` can remove the reference afterwards. `pr-policy` re-evaluates on
  `edited`, so the floor still catches it; this covers the form the loop reaches for.
- A `PreToolUse` refusal blocks the **whole** call, not the offending segment. Keep a
  `git push` on a call of its own rather than chained ahead of `gh pr create`.
"""

import json
import os
import re
import subprocess
import sys

from _shell import command_name, segments

# `pr-policy.yml`'s pattern, character for character. Kept in sync by a test rather than
# by discipline — there is no predicate to derive it from, so the test is what stands in,
# the same way one stands in for `no-unsliced-doc-reads`'s corpus globs.
_CLOSES = re.compile(r"(close[sd]?|fix(e[sd])?|resolve[sd]?) #[0-9]+", re.IGNORECASE)

_BRANCH = re.compile(r"^ralph/issue-(\d+)$")

_BODY_FLAGS = ("--body", "-b")
_BODY_FILE_FLAGS = ("--body-file", "-F")

# Flags that build the body out of this hook's sight: from the commits, in an editor, or
# in a browser. Present, and there is nothing to read and nothing to refuse.
_OPAQUE_FLAGS = ("--fill", "--fill-first", "--fill-verbose", "--web", "-w")


def _is_pr_create(segment: list[str]) -> bool:
    """`gh … pr create …`, with `pr create` adjacent wherever the flags put it.

    Adjacency rather than "both tokens appear": `gh -R owner/repo pr create` puts the
    repo in what a naive positional scan reads as an argument slot, and `gh pr list` after
    a `create` elsewhere in the line would otherwise match.
    """
    if command_name(segment) != "gh":
        return False
    return any(a == "pr" and b == "create" for a, b in zip(segment, segment[1:], strict=False))


def _body(segment: list[str], cwd: str) -> str | None:
    """The PR body as this hook can see it, or None when it cannot see one.

    None is the fail-open answer and covers every case at once: an opaque flag, no body
    flag at all, a body file that is stdin, and a body file that is not readable.
    """
    if any(token in _OPAQUE_FLAGS for token in segment):
        return None

    for index, token in enumerate(segment):
        following = segment[index + 1] if index + 1 < len(segment) else None
        if token in _BODY_FLAGS and following is not None:
            return following
        for flag in _BODY_FLAGS:
            if token.startswith(flag + "="):
                return token[len(flag) + 1 :]
        if token in _BODY_FILE_FLAGS and following is not None and following != "-":
            try:
                with open(os.path.join(cwd, os.path.expanduser(following))) as handle:
                    return handle.read()
            except OSError:
                return None
    return None


def _issue(cwd: str) -> str | None:
    """The issue number the current branch names, or None off a `ralph/issue-<N>` branch.

    A fabricated number is worse than none: it would send a merge at an unrelated issue.
    """
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd, capture_output=True, text=True
    )
    if result.returncode != 0:
        return None
    match = _BRANCH.match(result.stdout.strip())
    return match.group(1) if match else None


def _refusal(cwd: str) -> str:
    issue = _issue(cwd)
    line = f"Closes #{issue}" if issue else "Closes #<N>"
    derived = " — taken from the current branch" if issue else ""
    return (
        "Blocked: this `gh pr create` body has no issue-closing reference, so merging the "
        "PR would leave its issue open, and `pr-policy` fails the check for it.\n"
        f"Add this line to the body{derived}:\n\n"
        f"    {line}\n\n"
        "The merge is what closes an issue — never close one by hand (CLAUDE.md, PR policy)."
    )


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # An event this guard cannot read is not one it may block on.

    if event.get("tool_name") != "Bash":
        return 0

    command = event.get("tool_input", {}).get("command", "")
    cwd = event.get("cwd") or os.getcwd()

    for segment in segments(command):
        if not _is_pr_create(segment):
            continue
        body = _body(segment, cwd)
        if body is not None and not _CLOSES.search(body):
            print(_refusal(cwd), file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
