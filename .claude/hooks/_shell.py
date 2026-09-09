"""Shell-command reading shared by the Bash guards.

All three have to answer "which tokens of this command are commands, and which are the
paths they act on", and answering it once each would be three parsers that drift.
Imported as a sibling module: a hook runs as a script, so its own directory is
`sys.path[0]`.

Stdlib only, for the reason the hooks themselves are — this runs before `uv sync`.
"""

import shlex

# Everything that ends one command and begins another. Splitting on these is what lets a
# guard tell a program being *run* from a file being *read*: only the first token of a
# segment is in the executable position.
_SEPARATORS = ("|", "||", ";", "&&", "&", "(", ")")

# Programs that run *another* program rather than doing the work themselves. The token
# after one is the real subject: `sudo pip install` is a `pip install`, and a guard keyed
# on the first token reads `sudo`, matches nothing, and allows the call — every refusal
# bypassed by four letters, in the form that does the most damage.
_WRAPPERS = frozenset({"sudo", "doas", "command", "env"})

# Wrapper flags that consume the token after them, which would otherwise be read as the
# program: `sudo -u root pip install` runs `pip`, not `root`.
_WRAPPER_VALUE_FLAGS = frozenset(
    {
        "-u",
        "-g",
        "-p",
        "-C",
        "-h",
        "-r",
        "-t",
        "-U",
        "-D",
        "-R",
        "--user",
        "--group",
        "--prompt",
        "--chdir",
        "--host",
        "--role",
        "--type",
        "--close-from",
        "--other-user",
        "--unset",
    }
)

# Shell reserved words, and the two prefix commands that behave exactly like them. These
# stand where a program stands *without being one*, so a guard keyed on the first token
# reads `do` or `then`, matches nothing, and allows the call — the wrapper problem again,
# reached through the grammar rather than through a program. `for f in *.py; do sed -i …`
# is how one edit gets made across several files, which is the case the write guard is for.
#
# `if`, `while` and `until` are followed by a command directly, so peeling them is right.
# `for`, `case` and `select` are followed by a *name*, and peeling one would offer a loop
# variable as the program; they are deliberately absent. So are the closers (`done`, `fi`,
# `}`), which nothing runs behind.
_KEYWORDS = frozenset(
    {"!", "{", "do", "then", "elif", "else", "if", "while", "until", "time", "nohup", "exec"}
)

# Redirections that truncate or append to a named file. Every *shape* of one, not the two
# spellings that come to mind first: `punctuation_chars` groups a run of punctuation into
# a single token, so bash's both-streams `&>`/`&>>` and the noclobber-override `>|` arrive
# whole and equal neither `>` nor `>>`. A digit is not a punctuation char, so `2>` lexes
# as `2` then `>` and the plain form already covers it.
WRITE_REDIRECTS = (">", ">>", ">|", "&>", "&>>", "&>|")

# `>&` is the one shape that cannot be decided on the token alone: `>& file` writes both
# streams to a file, while the far commoner `2>&1` duplicates a descriptor and names no
# file at all. What follows tells them apart — a descriptor is a number, or `-` to close.
DUP_REDIRECT = ">&"

# A here-string's operand is the data itself, so it is dropped whole rather than offered
# as a path that happens to spell one. `<&` is deliberately absent: bash requires its
# operand to be a descriptor or `-`, so it can never name a file, and a branch that cannot
# change a verdict is one no test can fence.
_HERE_STRING = ("<<<",)

# An input redirect names a file the command **reads**, so only the operator is dropped
# and the operand stays a candidate: `cat < logs/run.log` spends the log exactly as
# `cat logs/run.log` does.
_INPUT_REDIRECTS = ("<", "<>")


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


def lines(command: str) -> list[str]:
    """Split on the newlines that end a command — the ones outside any quote.

    `str.splitlines` cannot tell those from the newlines *inside* a quoted argument, and
    a multi-line commit message or `--body` is one command however many lines it spans.
    Split blindly, an interior line of one lexes as a command of its own: a body quoting
    `echo x > tracked.py` was refused as a write, and one quoting `pip install httpx` as
    an install. The opening and closing lines hide the bug, since the unbalanced quote
    they carry makes the lexer refuse them and the guard fail open for the wrong reason.

    Quote state is all that is tracked. A backslash escapes the next character unless it
    is inside single quotes, where the shell gives it no meaning either.
    """
    out: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    escaped = False
    for char in command:
        if escaped:
            buf.append(char)
            escaped = False
        elif char == "\\" and quote != "'":
            buf.append(char)
            escaped = True
        elif quote:
            buf.append(char)
            if char == quote:
                quote = None
        elif char in "'\"":
            buf.append(char)
            quote = char
        elif char == "\n":
            out.append("".join(buf))
            buf = []
        else:
            buf.append(char)
    out.append("".join(buf))
    return out


def segments(command: str) -> list[list[str]]:
    """The command split into individual commands, empties dropped.

    `echo x | cat secret` is two segments, so `cat` is in the executable position of the
    second and `secret` is its argument. Without the split, everything after the first
    command reads as an argument and the executable-position exemption swallows the rest.

    **A newline ends a command as surely as a pipe does**, and the lexer cannot say so:
    `whitespace_split` discards newlines like any other space. So the split happens per
    line first, and the operators divide what is left. The guards found this the hard way
    on their first live call — a `tail -1` ending one line swallowed the program opening
    the next, and the read guard refused a command that read nothing.

    Which newlines, though, is `lines`' question rather than `str.splitlines`': one inside
    a quoted argument is data, and cutting there invents commands nobody ran.

    Taking the raw string rather than a token list is what makes that possible; a caller
    holding tokens has already lost the line breaks.

    **A heredoc body is data, not commands.** The lexer has no notion of one, so without
    the skip below every line of `cat <<'DOC' > /tmp/notes.md` … `DOC` is segmented as if
    it were being run — and a body quoting `echo hi > tracked.py` is then refused as a
    write to a file the command never opens. Creating a doc or a test whose fixtures are
    shell commands is exactly the case, and `tests/test_claude_hooks.py` is itself a file
    that could not be written that way.
    """
    out: list[list[str]] = []
    pending: list[str] = []  # heredoc terminators whose bodies are still being skipped
    for line in lines(command):
        if pending:
            if line.strip() == pending[0]:
                pending.pop(0)
            continue
        current: list[str] = []
        toks = tokens(line)
        i = 0
        while i < len(toks):
            tok = toks[i]
            # `<<-EOF` lexes as `<<` then `-EOF`, since `-` is not a punctuation char;
            # the leading dash is the tab-stripping form, not part of the terminator.
            if tok == "<<" and i + 1 < len(toks):
                pending.append(toks[i + 1].lstrip("-"))
                i += 2
                continue
            if tok in _SEPARATORS:
                if current:
                    out.append(current)
                current = []
            else:
                current.append(tok)
            i += 1
        if current:
            out.append(current)
    return out


def without_write_redirects(segment: list[str]) -> list[str]:
    """The segment with everything a redirect writes to removed.

    The mirror of `no-tracked-writes._write_targets`, off the same two constants so the
    shapes cannot drift apart: what that guard collects is what the two *read* guards
    have to discard. A redirect target is where output goes, and neither read guard has
    anything true to say about it — `cat src/x.py > logs/out.log` reads no log, and
    `cat README.md > docs/adr/0001.md` reads no ADR. Answering either with "that path is
    excluded" or "read it by section" is the misfire that teaches an agent to route
    around a guard.

    Three kinds, and the distinction between them is the whole reason this is not a
    blanket "drop the tail":

    - a **write** target, and a here-string's operand, which is the data itself, go with
      their operator;
    - an **input** redirect loses only its operator, because `< file` names a file being
      read and that is precisely what the callers are looking for;
    - the **descriptor prefix** of `2> err.log` goes too. `2` lexes as its own token, so
      leaving it behind offers a bare digit where a path would stand.

    What a redirect never does is end the command, so the tokens after one are still the
    program's own: `cat > out.log src/x.py` reads `src/x.py`.

    **The one ambiguity is a quoted operator.** `shlex` strips quotes, so the `'>'` of
    `grep '>' logs/run.log` arrives here as the operator token and nothing in the list
    distinguishes them. That is not a parse this can fail open on — both readings lex
    perfectly — so the callers order around it instead: `no-excluded-reads` takes the grep
    pattern out of the way before calling this, and `no-unsliced-doc-reads` has no
    pattern-taking program in its list to protect.
    """
    out: list[str] = []
    i = 0
    while i < len(segment):
        token = segment[i]
        following = segment[i + 1] if i + 1 < len(segment) else None

        opaque = token in WRITE_REDIRECTS or token in _HERE_STRING
        if token == DUP_REDIRECT and following is not None:
            # `2>&1` names a descriptor and was never a path; `>& file` is a write.
            opaque = not following.isdigit() and following != "-"
        if opaque or token in _INPUT_REDIRECTS:
            if out and out[-1].isdigit():
                out.pop()  # the `2` of `2> err.log`, standing where a path would
            i += 2 if opaque and following is not None else 1
            continue

        out.append(token)
        i += 1
    return out


def _is_assignment(token: str) -> bool:
    """Whether the token is a `VAR=value` prefix rather than the command itself."""
    return "=" in token and not token.startswith("=") and "/" not in token.split("=", 1)[0]


def unwrap(segment: list[str]) -> list[str]:
    """The segment from its real program onward — wrappers, keywords and assignments gone.

    `sudo -u root env FOO=1 pip install httpx` is a `pip install`, and so is the
    `do pip install httpx` of a loop body. Every guard here decides on the program, so
    each one has to see past whatever is standing in front of it — and neither case is
    the deliberate fail-open, since the lexer parses both perfectly and the guard simply
    reads the wrong token.

    A segment that is only a wrapper or a keyword (`sudo -v`, `done`) unwraps to nothing
    or to a word no rule names, which is the right answer for it.
    """
    rest = list(segment)
    while rest:
        if _is_assignment(rest[0]):
            rest = rest[1:]
            continue
        head = rest[0].rsplit("/", 1)[-1]
        if head not in _WRAPPERS and head not in _KEYWORDS:
            break
        rest = rest[1:]
        while rest and (_is_assignment(rest[0]) or rest[0].startswith("-")):
            takes_value = rest[0] in _WRAPPER_VALUE_FLAGS and len(rest) > 1
            rest = rest[2:] if takes_value else rest[1:]
    return rest


def command_name(segment: list[str]) -> str:
    """The program a segment runs, bare of its path, assignments, wrappers and keywords.

    `FOO=bar sudo /usr/bin/cat x` runs `cat`, and so does the `do cat x` of a loop body.
    A guard keyed on the raw first token would miss every assignment prefix, every
    wrapper and every reserved word; `unwrap` is where all three are peeled.
    """
    unwrapped = unwrap(segment)
    return unwrapped[0].rsplit("/", 1)[-1] if unwrapped else ""
