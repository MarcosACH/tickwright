"""Shell-command reading shared by the Bash guards.

Both guards have to answer "which tokens of this command are paths, and in what
position", and answering it twice would be two parsers that drift. Imported as a
sibling module: a hook runs as a script, so its own directory is `sys.path[0]`.

Stdlib only, for the reason the hooks themselves are — this runs before `uv sync`.
"""

import shlex

# Everything that ends one command and begins another. Splitting on these is what lets a
# guard tell a program being *run* from a file being *read*: only the first token of a
# segment is in the executable position.
_SEPARATORS = ("|", "||", ";", "&&", "&", "(", ")")


def tokens(command: str) -> list[str]:
    """Split a shell command, keeping operators as tokens of their own.

    `punctuation_chars` is what separates `> file` from `>file` and groups `>&` whole, so
    a redirect is distinguishable from the `2>&1` that merely looks like one.

    A command the lexer cannot split — an unbalanced quote, most often — yields nothing,
    and every guard built on this then allows the call. A guard that misfires on a parse
    it does not understand is one an agent learns to route around, which costs more than
    the call it wrongly blocked.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return []


def segments(toks: list[str]) -> list[list[str]]:
    """The command split into pipeline/list segments, empties dropped.

    `echo x | cat secret` is two segments, so `cat` is in the executable position of the
    second and `secret` is its argument. Without the split, everything after the first
    command reads as an argument and the executable-position exemption swallows the rest
    of the line.
    """
    out: list[list[str]] = []
    current: list[str] = []
    for tok in toks:
        if tok in _SEPARATORS:
            if current:
                out.append(current)
            current = []
        else:
            current.append(tok)
    if current:
        out.append(current)
    return out


def command_name(segment: list[str]) -> str:
    """The program a segment runs, bare of its path and of any leading assignments.

    `FOO=bar /usr/bin/cat x` runs `cat`. Environment assignments come first and are not
    the command; a guard keyed on the raw first token would miss every one of them.
    """
    for tok in segment:
        if "=" in tok and not tok.startswith("=") and "/" not in tok.split("=", 1)[0]:
            continue  # VAR=value prefix
        return tok.rsplit("/", 1)[-1]
    return ""


def arguments(segment: list[str]) -> list[str]:
    """A segment's non-flag arguments — everything that could name a path.

    Deliberately over-collects: a `grep` pattern lands here beside the file it searches.
    Both guards only act once git confirms a candidate, so a token that is not a path
    costs one lookup and changes no outcome.
    """
    seen_command = False
    args: list[str] = []
    for tok in segment:
        if not seen_command:
            if "=" in tok and not tok.startswith("=") and "/" not in tok.split("=", 1)[0]:
                continue
            seen_command = True
            continue
        if tok.startswith("-"):
            continue
        args.append(tok)
    return args
