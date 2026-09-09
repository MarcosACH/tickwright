#!/usr/bin/env python3
"""PreToolUse:Bash — refuse a shell write aimed at a file git already tracks.

Rewriting a file the harness has already read stales its cached copy, and the next
`Edit` against it warns that the file changed on disk, which costs a full re-read. A
one-line `sed -i` does that no less than a whole-file heredoc: diff size is irrelevant,
what matters is that the bytes moved underneath the cache.

`CLAUDE.md` used to ask for this in prose, and lost — the bypass-permissions guidance
pushing an agent toward Bash is re-injected on every turn, while a rule read once at
session start is not. Exit 2 does not argue.

**Tracked is the test**, because it is the mechanical reading of "a file that already
exists" — so creating a new file with a heredoc stays allowed, which is exactly the
exemption the prose granted. A file created earlier in the session and not yet committed
is untracked and therefore allowed too; the harness has no committed copy of it to stale.

Stdlib only, and `/usr/bin/env python3` rather than the project venv: this runs on a
fresh clone before `uv sync`, and the venv is not reliably on a hook's PATH.
"""

import json
import os
import subprocess
import sys

from _shell import command_name, segments, unwrap

# `sed` edits in place under any of these. The `-i.bak` form takes its suffix attached,
# so the flag is matched by prefix rather than by equality.
_SED_IN_PLACE = ("-i", "--in-place")

# Redirections that truncate or append to a named file. Every *shape* of one, not the two
# spellings that come to mind first: `punctuation_chars` groups a run of punctuation into
# a single token, so bash's both-streams `&>`/`&>>` and the noclobber-override `>|` arrive
# whole and equal neither `>` nor `>>`. A digit is not a punctuation char, so `2>` lexes
# as `2` then `>` and the plain form already covers it.
_WRITE_REDIRECTS = (">", ">>", ">|", "&>", "&>>", "&>|")

# `>&` is the one shape that cannot be decided on the token alone: `>& file` writes both
# streams to a file, while the far commoner `2>&1` duplicates a descriptor and names no
# file at all. What follows tells them apart — a descriptor is a number, or `-` to close.
_DUP_REDIRECT = ">&"


def _write_targets(segment: list[str]) -> list[str]:
    """Every token this one command could be writing to.

    Deliberately over-collects *within* a segment. A candidate only becomes a refusal
    once git confirms it is a tracked file, so a token that is not a path at all costs
    one cheap lookup and changes no outcome.

    **One command at a time is the whole point**, and within it, the program is read from
    the executable position. Over a whole shell line the in-place predicate finds a `sed`
    in one command and an `-i` in another and refuses `grep -i x tracked.py | sed
    's/a/b/'`; over a whole segment the `tee` predicate finds the word as an argument and
    refuses `grep -n tee CONTRIBUTING.md`. Neither writes anything, and both answer with
    the wrong instruction, since there is no edit there to move to the `Edit` tool.

    A redirect is the exception that stays positional: it writes wherever it appears.
    """
    targets: list[str] = []

    # Off the unwrapped remainder, so `sudo sed -i` is still a `sed -i`, while the `-i`
    # of `sudo -i sed 's/x/y/' f` — a login shell running a plain `sed` — is not read as
    # one. A wrapper changes who writes the file, not whether the cached copy goes stale.
    unwrapped = unwrap(segment)
    if command_name(segment) == "sed" and any(
        tok.startswith(_SED_IN_PLACE) for tok in unwrapped[1:]
    ):
        # `sed -i '' 's/x/y/' file` puts the target last, but the flag forms differ per
        # platform and the script itself may be several arguments. Offer them all.
        targets.extend(unwrapped)

    if command_name(segment) == "tee":
        # Everything up to the next redirect is a file `tee` writes; the pipe that would
        # also end the list has already ended the segment. Off the unwrapped remainder,
        # and keyed on the **executable position** for the same reason `sed` is: a `tee`
        # standing in an argument is a word being searched for, not a program being run,
        # and `grep -n tee CONTRIBUTING.md` writes nothing.
        for tok in unwrapped[1:]:
            if tok.startswith("-"):
                continue
            if not tok or tok[0] in "|;&<>":
                break
            targets.append(tok)

    for i, tok in enumerate(segment):
        if i + 1 >= len(segment):
            break
        following = segment[i + 1]
        if tok in _WRITE_REDIRECTS:
            targets.append(following)
        elif tok == _DUP_REDIRECT and not following.isdigit() and following != "-":
            targets.append(following)

    return targets


def _tracked_files(cwd: str, candidates: list[str]) -> list[str]:
    """The candidates git reports as tracked files, in the repo-relative form it uses.

    Each is asked for on its own with `--error-unmatch` so a candidate that is not a path
    fails alone, and **every** entry git answers with is a write target. A glob is a
    pathspec git resolves — `sed -i '' 's/x/y/' src/*.py` rewrites each file it matches —
    so counting the answers would allow the write in proportion to how many files it
    touches, and the multi-file edit is the case this guard exists for.

    A **directory** is the one candidate whose entries are not targets: git answers a
    directory pathspec with everything beneath it, and none of them is being written.
    That is asked directly rather than inferred from the answer's size.
    """
    found: list[str] = []
    for cand in candidates:
        if not cand or cand.startswith("-") or cand.startswith(":"):
            continue
        path = os.path.normpath(os.path.join(cwd, os.path.expanduser(cand)))
        if os.path.isdir(path):
            continue
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "-z", "--", path],
            cwd=cwd,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            continue
        for entry in result.stdout.split("\0"):
            if entry and entry not in found:
                found.append(entry)
    return found


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # An event this guard cannot read is not one it may block on.

    # The matcher already narrows to Bash. Checking anyway keeps the guard correct the
    # day the matcher is widened, which is cheaper than finding out that it was not.
    if event.get("tool_name") != "Bash":
        return 0

    command = event.get("tool_input", {}).get("command", "")
    cwd = event.get("cwd") or os.getcwd()

    candidates: list[str] = []
    for segment in segments(command):
        candidates.extend(_write_targets(segment))

    tracked = _tracked_files(cwd, candidates)
    if not tracked:
        return 0

    print(
        f"Blocked: this writes to {', '.join(tracked)}, which git already tracks.\n"
        "Use the Edit tool instead — a Bash write stales the harness's cached copy of "
        "the file, and the next Edit against it costs a full re-read.\n"
        "Heredocs and `sed -i` are for creating new files.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
