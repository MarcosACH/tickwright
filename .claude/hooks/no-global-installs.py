#!/usr/bin/env python3
"""PreToolUse:Bash — refuse an install that lands outside the project venv.

Dependencies here are managed with `uv` against `.venv`, and the maintainer's standing
rule is that nothing is ever installed into the global or system environment.

That rule earns a guard rather than a paragraph because its damage is **off-repo**. A
bad edit is caught by `ci`, reverted with the branch, and gone. A package installed into
the system Python survives the branch, the PR and the revert, and no check in this
repository can see that it happened — the one class of mistake the gate is structurally
blind to is the one worth blocking before it runs.

The line is *where it lands*, not which tool spelled it:

- `uv pip install` writes into the project venv and is fine; `--system` is not.
- `uv tool run` and `uvx` are ephemeral and durable-install nothing; `uv tool install`
  is durable and is not.
- `npm install` is project-local; `npm install -g` is not.
"""

import json
import sys

from _shell import command_name, segments, tokens

_SANCTIONED = "uv add <pkg> (or `uv sync` to install what the lockfile already names)"

# A program reached through one of these is inside a virtualenv, so an install through
# it is project-local whatever the program is called.
_VENV_MARKERS = ("venv/", "virtualenv/")


def _program(segment: list[str]) -> str:
    """The segment's program token, whole — path included, unlike `command_name`.

    The path is the evidence: `.venv/bin/pip install` and `pip install` differ in nothing
    else, and only one of them is the rule's subject.
    """
    for tok in segment:
        if "=" in tok and not tok.startswith("=") and "/" not in tok.split("=", 1)[0]:
            continue  # VAR=value prefix
        return tok
    return ""


def _in_venv(program: str) -> bool:
    return any(marker in program for marker in _VENV_MARKERS)


def _rest(segment: list[str]) -> list[str]:
    """The segment after its program token, flags included.

    Flags are kept because this guard decides *on* them — `--system` and `-g` are the
    whole question — so `_shell.arguments`, which drops them, is the wrong reading here.
    """
    program = _program(segment)
    return segment[segment.index(program) + 1 :] if program in segment else []


def _refusal(segment: list[str]) -> str | None:
    """Why this segment installs outside the project venv, or None if it does not."""
    name = command_name(segment)
    program = _program(segment)
    rest = _rest(segment)
    args = [tok for tok in rest if not tok.startswith("-")]

    if name in ("pip", "pip3") and "install" in args and not _in_venv(program):
        return "`pip install` targets whichever Python is on PATH, not the project venv"

    if name.startswith("python") and not _in_venv(program):
        if "-m" in rest and "pip" in args and "install" in args:
            return "`python -m pip install` targets whichever Python is on PATH"

    if name == "uv":
        if args[:1] == ["pip"] and "--system" in rest:
            return "`uv pip install --system` is the one uv form that skips the venv"
        if args[:2] == ["tool", "install"]:
            return "`uv tool install` is a durable machine-wide install; `uv tool run` is not"

    if name == "pipx" and args[:1] == ["install"]:
        return "`pipx install` is machine-wide by design"

    if name == "brew" and args[:1] == ["install"]:
        return "`brew install` is a system package manager"

    if name in ("npm", "pnpm", "yarn") and args[:1] in (["install"], ["i"], ["add"]):
        if "-g" in rest or "--global" in rest:
            return f"`{name}` with -g/--global installs machine-wide"

    return None


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # An event this guard cannot read is not one it may block on.

    if event.get("tool_name") != "Bash":
        return 0

    command = event.get("tool_input", {}).get("command", "")
    reasons = [
        reason for segment in segments(tokens(command)) if (reason := _refusal(segment)) is not None
    ]
    if not reasons:
        return 0

    print(
        "Blocked: " + "; ".join(reasons) + ".\n"
        f"This project installs into its own .venv with uv: {_SANCTIONED}.\n"
        "If a global install is genuinely unavoidable, stop and ask the maintainer first.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
