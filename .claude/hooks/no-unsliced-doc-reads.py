#!/usr/bin/env python3
"""PreToolUse:Read — refuse a whole read of the long-form corpus.

This repo carries ~140k words across 50 ADRs, `CONTEXT.md`, the module maps and the
research notes. Reading one whole is most of a planning budget, so `CLAUDE.md` asks for
`.agents/tools/doc-slice` instead. That request had the shape every rule this project has
since converted into a guard had: **complying costs an extra tool call and ignoring it
costs nothing**, so the cheap path was the wrong one and nothing charged for it.

Nothing goes red when it is ignored, either. The window is simply gone — and on an ADR
something worse has happened, because `docs/adr/` is append-corrected and document order
hands back the *superseded* decision as if it were current.

The escape is deliberate. A `Read` carrying an explicit `offset`/`limit` is allowed
through, including one wide enough to span the file. The rule being enforced is that a
whole read is an act someone chose, not the default shape of the call; a guard with no
deliberate escape is one an agent routes around, which costs more than the call it
wrongly blocked.
"""

import fnmatch
import json
import os
import subprocess
import sys

# The four families `CLAUDE.md` already names. Unlike the two path guards, this list is
# not derived from git: "long enough to be worth slicing" is an editorial judgement and
# git has no predicate for it. `tests/test_claude_hooks.py` asserts every glob still
# matches a real file, so a renamed directory fails a test rather than silently
# disarming the guard.
_CORPUS = (
    "docs/adr/*.md",
    "CONTEXT.md",
    "docs/module-maps/*.md",
    "docs/research/*.md",
)

_DOC_SLICE = ".agents/tools/doc-slice"


def _in_corpus(relative: str) -> bool:
    """Whether a repo-relative path is one of the corpus files.

    The depth comparison is load-bearing: `fnmatch`'s `*` crosses `/` happily, so
    `docs/adr/*.md` would otherwise claim `docs/adr/archive/0001-old.md` as well.
    """
    return any(
        fnmatch.fnmatch(relative, glob) and relative.count("/") == glob.count("/")
        for glob in _CORPUS
    )


def _repo_root(cwd: str) -> str | None:
    """The repo `cwd` sits in, or None when it is not in one."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _slice(tool: str, *args: str) -> tuple[int, str]:
    result = subprocess.run([tool, *args], capture_output=True, text=True)
    return result.returncode, result.stdout


def _amendment_counts(tool: str, path: str) -> dict[str, int]:
    """How many amendment blocks each section carries, keyed on its heading text.

    `--amendments` exits **3** on a file outside the append-corrected convention — the
    research notes, where `**(` is ordinary bold prose opening a block that never closes.
    That means *out of domain*, not malformed, so it yields no counts and no annotation
    rather than costing the caller its table of contents.
    """
    code, out = _slice(tool, "--amendments", path)
    if code != 0:
        return {}

    counts: dict[str, int] = {}
    for line in out.splitlines():
        if not line.startswith("--- "):
            continue
        banner = line[4:].split(None, 1)  # "<line>  <heading>"
        if len(banner) == 2:
            counts[banner[1]] = counts.get(banner[1], 0) + 1
    return counts


def _annotated_toc(tool: str, path: str) -> str | None:
    """The file's table of contents, each corrected section marked `(+N)`.

    None when there is nothing to offer, which is the one case this guard stays quiet in:
    a refusal that hands back no index is an obstacle rather than a guard.
    """
    code, out = _slice(tool, path)
    if code != 0 or not out.strip():
        return None

    counts = _amendment_counts(tool, path)
    lines = []
    for line in out.splitlines():
        heading = line.split(None, 2)  # "<line>  <level>  <heading>"
        n = counts.get(heading[2]) if len(heading) == 3 else None
        lines.append(f"{line}  (+{n})" if n else line)
    return "\n".join(lines)


def _refusal(path: str, toc: str, corrected: bool) -> str:
    correction = (
        "\nA section marked (+N) carries N amendment blocks. This corpus is "
        "append-corrected: those blocks hold the current truth and the prose above them "
        "is often the retired version, so read a marked section's amendments first.\n"
        if corrected
        else ""
    )
    return (
        f"Blocked: {path} is read by section, not whole. Its table of contents:\n\n"
        f"{toc}\n"
        f"{correction}\n"
        f"  {_DOC_SLICE} {path} <heading-substr>\n"
        f"  {_DOC_SLICE} --amendments {path} <heading-substr>\n\n"
        "If you genuinely need the raw lines, Read it again with an explicit offset/limit."
    )


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # An event this guard cannot read is not one it may block on.

    if event.get("tool_name") != "Read":
        return 0

    tool_input = event.get("tool_input", {})
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return 0

    # The escape, checked before anything expensive.
    if tool_input.get("offset") is not None or tool_input.get("limit") is not None:
        return 0

    cwd = event.get("cwd") or os.getcwd()
    root = _repo_root(cwd)
    if root is None:
        return 0

    absolute = os.path.normpath(os.path.join(cwd, os.path.expanduser(file_path)))
    if not absolute.startswith(root + os.sep) or not os.path.isfile(absolute):
        return 0

    relative = os.path.relpath(absolute, root)
    if not _in_corpus(relative):
        return 0

    tool = os.path.join(root, _DOC_SLICE)
    if not os.access(tool, os.X_OK):
        return 0

    toc = _annotated_toc(tool, absolute)
    if toc is None:
        return 0

    print(_refusal(relative, toc, corrected="(+" in toc), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
