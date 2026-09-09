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

from _shell import tokens

# `sed` edits in place under any of these. The `-i.bak` form takes its suffix attached,
# so the flag is matched by prefix rather than by equality.
_SED_IN_PLACE = ("-i", "--in-place")

# Redirections that truncate or append to a named file. `2>&1` lexes as `2`, `>&`, `1`
# under `punctuation_chars`, so it never reaches this set — which is the commonest false
# positive a naive scan for `>` produces.
_WRITE_REDIRECTS = (">", ">>")


def _write_targets(toks: list[str]) -> list[str]:
    """Every token the command could be writing to.

    Deliberately over-collects. A candidate only becomes a refusal once git confirms it
    is a tracked file, so a token that is not a path at all costs one cheap lookup and
    changes no outcome.
    """
    targets: list[str] = []

    sed_in_place = any(tok == "sed" or tok.endswith("/sed") for tok in toks) and any(
        tok.startswith(_SED_IN_PLACE) for tok in toks
    )
    if sed_in_place:
        # `sed -i '' 's/x/y/' file` puts the target last, but the flag forms differ per
        # platform and the script itself may be several arguments. Offer them all.
        targets.extend(toks)

    for i, tok in enumerate(toks):
        if tok in _WRITE_REDIRECTS and i + 1 < len(toks):
            targets.append(toks[i + 1])
        if tok == "tee" or tok.endswith("/tee"):
            # Everything up to the next pipe or redirect is a file `tee` writes.
            for nxt in toks[i + 1 :]:
                if nxt.startswith("-"):
                    continue
                if not nxt or nxt[0] in "|;&<>":
                    break
                targets.append(nxt)

    return targets


def _tracked_files(cwd: str, candidates: list[str]) -> list[str]:
    """The candidates git reports as tracked files, in the repo-relative form it uses.

    Each is asked for on its own with `--error-unmatch` so a candidate that is not a path
    fails alone. The answer must be a *single* entry equal to the candidate itself: a
    directory pathspec matches every file beneath it, and that is not a write target.
    """
    found: list[str] = []
    for cand in candidates:
        if not cand or cand.startswith("-") or cand.startswith(":"):
            continue
        path = os.path.normpath(os.path.join(cwd, os.path.expanduser(cand)))
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "-z", "--", path],
            cwd=cwd,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            continue
        entries = [e for e in result.stdout.split("\0") if e]
        if len(entries) == 1 and entries[0] not in found:
            found.append(entries[0])
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

    tracked = _tracked_files(cwd, _write_targets(tokens(command)))
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
