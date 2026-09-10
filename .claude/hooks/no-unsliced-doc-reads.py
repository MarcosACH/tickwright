#!/usr/bin/env python3
"""PreToolUse:Read|Bash — refuse a whole read of the long-form corpus.

This repo carries ~140k words across 50 ADRs, `CONTEXT.md`, the module maps and the
research notes. Reading one whole is most of a planning budget, so `CLAUDE.md` asks for
`.agents/tools/doc-slice` instead. That request had the shape every rule this project has
since converted into a guard had: **complying costs an extra tool call and ignoring it
costs nothing**, so the cheap path was the wrong one and nothing charged for it.

Nothing goes red when it is ignored, either. The window is simply gone — and on an ADR
something worse has happened, because `docs/adr/` is append-corrected and document order
hands back the *superseded* decision as if it were current.

**Both doors, because one of them proves nothing.** A guard bound to `Read` alone is
evaded by `cat`, which loads the identical bytes through Bash — the evasion the eval case
this guard replaced graded explicitly, and the door bypass-permissions mode actively
pushes an agent toward. So the Bash arm refuses the whole-file *dumpers* and leaves the
bounded readers alone.

The escape is deliberate. A `Read` carrying an explicit `offset`/`limit` is allowed
through, including one wide enough to span the file. The rule being enforced is that a
whole read is an act someone chose, not the default shape of the call; a guard with no
deliberate escape is one an agent routes around, which costs more than the call it
wrongly blocked.

A refusal is never bare: it carries the file's own index — an annotated table of contents,
or for the glossary its terms and their line numbers. Complying then costs one turn rather
than two, which was the asymmetry that made the prose version of this rule lose.
"""

import fnmatch
import glob
import json
import os
import re
import subprocess
import sys

from _shell import command_name, segments, unwrap, without_write_redirects

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

# `CONTEXT.md` is the one corpus file whose units are not headings: its `Language` section
# is a single h2 running 770 of 810 lines, so a table of contents of it is not an index of
# it, and offering one would send the caller to `doc-slice CONTEXT.md Language` — which
# returns the file it was just refused. Its 45 bold terms are the index (1,086 characters
# against 61,122), each with the line number the offset/limit escape takes.
#
# Named, not inferred. An ADR's headings *are* its units, and scanning one for
# bold-prefixed lines would index its emphasis instead.
_TERM_INDEXED = ("CONTEXT.md",)
_TERM = re.compile(r"^\*\*([^*]+)\*\*:")

# Programs that load a file **whole**. Deliberately narrower than
# `no-excluded-reads._READERS`, and a separate list rather than a shared one: that guard
# asks "is this a read at all", over paths nothing may read; this one asks "is this an
# *unbounded* read", over paths that are read constantly and correctly. Merging them
# would make `grep` unusable on an ADR, which is the opposite of the point.
#
# `head`, `tail`, `sed -n`, `grep`, `rg` and `wc` are absent on purpose: they are already
# the bounded read the rule asks for, so bounding one is compliance rather than evasion.
_DUMPERS = frozenset({"cat", "bat", "less", "more", "nl", "od", "xxd", "strings"})


def _in_corpus(relative: str) -> bool:
    """Whether a repo-relative path is one of the corpus files.

    The depth comparison is load-bearing: `fnmatch`'s `*` crosses `/` happily, so
    `docs/adr/*.md` would otherwise claim `docs/adr/archive/0001-old.md` as well.
    """
    return any(
        fnmatch.fnmatch(relative, pattern) and relative.count("/") == pattern.count("/")
        for pattern in _CORPUS
    )


def _expand(cwd: str, candidate: str) -> list[str]:
    """The paths a candidate token names, with any glob resolved.

    `shlex` hands a pattern through as the literal it lexed, and `os.path.isfile` then
    says no — so `cat docs/adr/*.md` went through, which on the real repo is fifty ADRs
    at once and the largest single spend the corpus allows. `no-excluded-reads` never had
    the gap because `git check-ignore` takes a pathspec and resolves it itself; this
    guard has no git predicate to ask, so the expansion happens here.

    A pattern matching nothing falls back to the literal, which is what the shell does
    with an unmatched glob and what the caller below already answers correctly.

    `~` is expanded here rather than left to `_corpus_member`, which is the only other
    place that knows about it. `glob` does not expand a user prefix — it matches nothing
    and the fall-back literal then fails `isfile` on the `*` it still carries — so a
    guard that expanded it only on the resolved path refused `~/repo/CONTEXT.md` and
    allowed `~/repo/docs/adr/*.md`, which is the larger read of the two.
    """
    expanded = os.path.expanduser(candidate)
    if not any(char in expanded for char in "*?["):
        return [expanded]
    return sorted(glob.glob(expanded, root_dir=cwd)) or [expanded]


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


def _term_index(absolute: str) -> str | None:
    """The glossary's terms with their line numbers, or None if it carries none."""
    entries = []
    with open(absolute, encoding="utf-8", errors="replace") as handle:
        for number, line in enumerate(handle, 1):
            match = _TERM.match(line)
            if match:
                entries.append(f"{number:5d}  {match.group(1)}")
    return "\n".join(entries) or None


def _glossary_refusal(path: str, index: str) -> str:
    return (
        f"Blocked: {path} is read by term, not whole. Its terms, with line numbers:\n\n"
        f"{index}\n\n"
        f'  Read(file_path="{path}", offset=<line>, limit=20)  the entry you need\n'
        f"  {_DOC_SLICE} {path} <heading-substr>              a section other than the glossary"
    )


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


def _bulk_refusal(root: str, members: list[str]) -> str | None:
    """The refusal for a call naming several corpus files at once.

    One index answers a whole read; several cost more than the read they refused —
    ADR-0040's table of contents alone is 1,292 characters, so the fifty a
    `cat docs/adr/*.md` would earn are worse than the file the guard was protecting.
    Past one file the answer is the list and a request to choose, which is also the only
    honest one: nothing here can guess which of them was wanted.

    None when `doc-slice` is missing, for the reason the single-file path fails open on
    it — what this offers is that tool's output, and a refusal pointing at something the
    repo does not have is an obstacle rather than a guard.
    """
    if not os.access(os.path.join(root, _DOC_SLICE), os.X_OK):
        return None
    listing = "\n".join(f"  {member}" for member in members)
    return (
        f"Blocked: that reads {len(members)} files of the sliced corpus whole:\n\n"
        f"{listing}\n\n"
        "Name one on its own and the refusal answers with its index, or go straight to a "
        f"section:\n\n  {_DOC_SLICE} <file> <heading-substr>"
    )


def _corpus_member(root: str, cwd: str, path: str) -> str | None:
    """The repo-relative path when it is a corpus file, else None.

    Split from the refusal below because deciding *whether* to block has to stay cheap:
    an expanded glob asks this of every file it matched, and pricing that in a
    `doc-slice` subprocess each would spend more than the read being refused.
    """
    absolute = os.path.normpath(os.path.join(cwd, os.path.expanduser(path)))
    if not absolute.startswith(root + os.sep) or not os.path.isfile(absolute):
        return None

    relative = os.path.relpath(absolute, root)
    return relative if _in_corpus(relative) else None


def _refusal_for(root: str, relative: str) -> str | None:
    """The reason a corpus file may not be loaded whole, or None if it may be."""
    absolute = os.path.join(root, relative)
    if relative in _TERM_INDEXED:
        index = _term_index(absolute)
        return None if index is None else _glossary_refusal(relative, index)

    tool = os.path.join(root, _DOC_SLICE)
    if not os.access(tool, os.X_OK):
        return None

    toc = _annotated_toc(tool, absolute)
    return None if toc is None else _refusal(relative, toc, corrected="(+" in toc)


def _read_candidate(tool_input: dict[str, object]) -> str | None:
    """The path a `Read` would load whole, or None when it would not load one."""
    # The escape, checked before anything expensive.
    if tool_input.get("offset") is not None or tool_input.get("limit") is not None:
        return None
    path = tool_input.get("file_path")
    return path if isinstance(path, str) and path else None


def _dump_candidates(command: str) -> list[str]:
    """The paths a shell command would load whole.

    `unwrap` peels the assignments, wrappers and reserved words standing in front of the
    program, so `sudo cat CONTEXT.md` and the `do cat "$f"` of a loop body are read at
    the same token `command_name` decided on.

    `without_write_redirects` drops what the command writes to, which is not a file it
    reads: `cat README.md > docs/adr/0001.md` opens no ADR, and refusing it would answer
    a write with "read it by section instead" — an instruction that does not apply, about
    a file `no-tracked-writes` has something true to say about instead.

    It runs **before** `unwrap` here, unlike in `no-excluded-reads`, and the difference is
    `_DUMPERS` rather than taste: nothing in it takes a *pattern*, so there is no quoted
    `'>'` to be mistaken for an operator and no reason to defer the scan.

    **One strip feeds both reads, the program's name included.** A redirect standing
    *before* the command it belongs to occupies the executable position exactly as
    `sudo` and a loop's `do` do, so `2>/dev/null cat notes.md` names the bare `2` and
    matches nothing while the whole file is spent anyway. `unwrap` peels that shape for
    every guard now, which makes naming off `stripped` here the same answer reached
    twice — kept because it is the one this function can be read without leaving.

    Over-collects the rest: a value riding a flag (`nl -w 5 notes.md`) lands here beside
    the file, which costs one `isfile` and changes no outcome.
    """
    candidates: list[str] = []
    for segment in segments(command):
        stripped = without_write_redirects(segment)
        if command_name(stripped) not in _DUMPERS:
            continue
        candidates.extend(token for token in unwrap(stripped)[1:] if not token.startswith("-"))
    return candidates


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # An event this guard cannot read is not one it may block on.

    tool_input = event.get("tool_input", {})
    tool_name = event.get("tool_name")
    if tool_name == "Read":
        candidate = _read_candidate(tool_input)
        candidates = [candidate] if candidate else []
    elif tool_name == "Bash":
        candidates = _dump_candidates(tool_input.get("command", ""))
    else:
        return 0

    if not candidates:
        return 0

    cwd = event.get("cwd") or os.getcwd()
    root = _repo_root(cwd)
    if root is None:
        return 0

    # `dict.fromkeys` rather than a set: two globs can name one file, and the listing a
    # bulk refusal prints should read in the order the command did.
    members = list(
        dict.fromkeys(
            member
            for candidate in candidates
            for path in _expand(cwd, candidate)
            if (member := _corpus_member(root, cwd, path)) is not None
        )
    )
    if not members:
        return 0

    reason = _bulk_refusal(root, members) if len(members) > 1 else _refusal_for(root, members[0])
    if reason is None:
        return 0

    print(reason, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
