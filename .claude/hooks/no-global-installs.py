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

from _shell import command_name, segments, unwrap

_SANCTIONED = "uv add <pkg> (or `uv sync` to install what the lockfile already names)"

# A program reached through one of these is inside a virtualenv, so an install through
# it is project-local whatever the program is called.
_VENV_MARKERS = ("venv/", "virtualenv/")


def _in_venv(program: str) -> bool:
    return any(marker in program for marker in _VENV_MARKERS)


def _refusal(segment: list[str]) -> str | None:
    """Why this segment installs outside the project venv, or None if it does not.

    Read off `unwrap`, so a wrapper is peeled before the decision: `sudo pip install` is
    a `pip install`, and it is the form that does the most damage — a root install into
    the system Python, which no check in this repository can see afterwards.
    """
    unwrapped = unwrap(segment)
    if not unwrapped:
        return None

    # The program token whole, path included: `.venv/bin/pip install` and `pip install`
    # differ in nothing else, and only one of them is the rule's subject. Flags are kept
    # in `rest` because this guard decides *on* them — `--system` and `-g` are the whole
    # question — while `args` is the non-flag reading the subcommand tests want.
    program = unwrapped[0]
    name = command_name(segment)
    rest = unwrapped[1:]
    args = [tok for tok in rest if not tok.startswith("-")]

    if name in ("pip", "pip3") and "install" in args and not _in_venv(program):
        return "`pip install` targets whichever Python is on PATH, not the project venv"

    if name.startswith("python") and not _in_venv(program):
        if "-m" in rest and "pip" in args and "install" in args:
            return "`python -m pip install` targets whichever Python is on PATH"

    if name == "uv":
        # The verb, not just the flag: `--system` also scopes a query, and refusing
        # `uv pip list --system` would answer with a sentence naming a command that was
        # never run. Every other branch here gates on the subcommand the same way.
        if args[:2] == ["pip", "install"] and "--system" in rest:
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
    reasons = [reason for segment in segments(command) if (reason := _refusal(segment)) is not None]
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
