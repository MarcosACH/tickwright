# Hooks

Two sets of hooks catch mistakes before CI does. Neither is the gate. CI re-runs everything and
`--no-verify` cannot skip it.

- Git hooks in `.githooks/` fire at commit and push.
- Claude Code hooks in `.claude/hooks/` fire at each tool call.

## Git hooks

Enable them once per clone with `git config core.hooksPath .githooks`. Linked worktrees share
the setting.

| Hook | Does |
| ---- | ---- |
| `pre-commit` | Runs `ruff check --fix` and `ruff format` on staged Python, then re-stages it. Unstaged hunks are kept out of the commit. |
| `commit-msg` | Runs the local hook guard if one is installed. Otherwise does nothing. |
| `pre-push` | Runs `mypy` on changed Python before the push leaves your machine. |

### Local hook guard

`pre-commit` and `commit-msg` each end by calling an optional executable that lives outside the
repo. It is never published. Nothing installs one, and you do not need one. If you add one, this
is the contract:

- **Location.** `hooks-local/<hook-name>` under the common git directory, so
  `.git/hooks-local/pre-commit` and `.git/hooks-local/commit-msg`.
- **Arming.** The executable bit. A missing or non-executable guard is skipped in silence.
- **Authority.** A non-zero exit aborts the commit. That veto is final.

The maintainer's private reference scrub hangs off this seam. It backs the "no copied code" rule
in `CLAUDE.md`. `tests/test_githooks.py` fences the contract against a real `git`.

### Per-worktree and shared git state

Linked worktrees share one common git directory. Config, `refs/stash`, and `hooks-local/` live
there. Each worktree also has a private directory at `.git/worktrees/<name>`. A hook must pick
one on purpose.

- **Shared state** is resolved with `git rev-parse --git-common-dir`. The local hook guard is
  looked up there, so one install serves every worktree.
- **Per-worktree scratch** must stay private, or two concurrent hooks overwrite each other.
  `pre-commit` writes its unstaged patch under `--git-dir`. `pre-push` parks its autostash under
  `refs/worktree/`, off the shared stash stack. `tests/test_githooks.py` fences the `pre-push`
  case against a real linked worktree.

## Claude Code hooks

Git hooks fire at commit time. An agent breaks a rule many tool calls earlier, and by then the
cost is paid. `.claude/hooks/` closes that gap. Seven scripts are wired in the committed
`.claude/settings.json`. Each reads the event on stdin.

### Five guards refuse

They run on `PreToolUse`. Exit `0` allows. Exit `2` blocks, with the reason on stderr.

| Guard | Binds | Refuses | Still allows |
| ----- | ----- | ------- | ------------ |
| `no-tracked-writes` | `Bash` | `sed -i`, a redirect, or `tee` aimed at a git-tracked file, or at a glob that matches one | creating a new file, `2>&1` and `>&2`, `tee` as a grep pattern, a heredoc body that quotes a write, a directory, anything outside the repo |
| `no-excluded-reads` | `Bash` | reading a path that `git check-ignore` matches | `.agents/plans/`, `.agents/verify/`, `prototypes/`, a program in the executable position such as `.venv/bin/ruff`, a grep pattern, a redirect target |
| `no-global-installs` | `Bash` | `pip install`, `uv pip install --system`, `uv tool install`, `pipx`, `brew`, `npm -g`, also behind `sudo` or inside a loop | `uv add`, `uv sync`, `uvx`, `uv tool run`, `uv pip install` without `--system` |
| `no-unsliced-doc-reads` | `Read`, `Bash` | a whole read of `CONTEXT.md`, an ADR, a module map, or a research note, including through a glob | `head`, `tail`, `sed -n`, `grep`, `doc-slice`, a `Read` with `offset` and `limit`, a redirect target |
| `no-unlinked-prs` | `Bash` | a `gh pr create` whose `--body` or `--body-file` has no `Closes #N` | any body it cannot see: `--fill`, `--web`, an editor, `-F -`, or an unexpanded `"$BODY"` |

A heredoc body is visible, so `no-unlinked-prs` judges it. It reads the body the way the shell
does: a terminator counts only in column 0, and the last one ends the body. A quoted `EOF` inside
the body does not cut it short and drop the `Closes #N` after it.

Two guards answer as well as refuse. `no-unsliced-doc-reads` returns the file's table of contents
from `doc-slice`, with each section marked `(+N)` for its amendment blocks. For `CONTEXT.md` it
returns the bold terms and their line numbers instead, since that file's units are not headings.
`no-unlinked-prs` returns the `Closes #<N>` line the `ralph/issue-<N>` branch implies. Before
that, the wrong path was the cheap one. Answering makes both paths cost the same.

### Two hooks answer

They run on events with no veto.

| Hook | Event | Does |
| ---- | ----- | ---- |
| `ruff-on-write` | `PostToolUse` on `Edit` and `Write` | Runs `ruff check --fix`, then `ruff format`, on the one `.py` file just written. Reports the rewrite and anything it could not fix. |
| `resume-from-plan` | `SessionStart` | Prints the open behaviors of the current slice, read from `ralph/issue-<N>` and `.agents/plans/issue-<N>.md`. |

`ruff-on-write` fixes before it formats. That is ruff's documented order. A fix applied after
formatting is never formatted. Findings are re-read after the format, so line numbers point at
the file as it now is.

`ruff-on-write` does not run mypy. A red TDD step is allowed to type-error, and a note on every
edit is one the agent learns to skim. CI keeps mypy.

### Design rules

- **Committed, not local.** A hook in `settings.local.json` reaches one machine. The point is to
  reach other people's agents too.
- **Derived, not listed.** The path guards ask `git`. The PR hook reads the issue number off the
  branch. A copy of `.gitignore` inside a hook would be two lists that drift. Two exceptions
  have no predicate to derive from: the doc corpus of `no-unsliced-doc-reads`, and the `Closes`
  pattern of `no-unlinked-prs`, which must match `pr-policy.yml`. Each is held by a test instead.
- **Fail open on a parse, never on a position.** A command the lexer cannot parse goes through.
  A hidden body is not judged. A missing `.venv/bin/ruff` is silence. But a token the guard can
  read is resolved: `sudo pip install`, a reserved word in front of the program, a redirect
  written before it, a grep pattern, and a heredoc body are all handled, not shrugged at.
- **`.venv/bin/<tool>`, never `uv run <tool>`.** `uv run` re-syncs first, about 2 seconds
  against 13 ms. A slow hook is one someone turns off.

### Known gaps

These are whole-file reads or writes that go through today. They are listed so nobody
rediscovers them one at a time.

- A value the shell has not expanded: `sed -i s/a/b/ $f`, `$HOME/repo/CONTEXT.md`.
- A leading input redirect: `< CONTEXT.md cat`.
- Brace expansion: `docs/{adr,module-maps}/*.md`.
- A `cd` in an earlier segment of the same command.

### Trust and tests

The hooks are plain stdlib Python with no network access. Read them before you trust them.
`tests/test_claude_hooks.py` fences all seven, and checks the wiring in `settings.json` under
every event key. Hook config is read at session start, so restart Claude Code after changing one.
