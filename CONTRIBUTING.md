# Contributing to Tickwright

Thanks for your interest in Tickwright — a readable, event-driven algorithmic trading engine
built as a reference implementation. **Clarity and correctness are the priorities**; please keep
changes small, well-tested, and easy to read.

## Before you start

- Read [`CONTEXT.md`](CONTEXT.md) — the project's vocabulary, every term resolved.
- Skim the relevant [`docs/adr/`](docs/adr/) — the locked-in architectural decisions and *why*.

## Development setup

- **Python 3.13**, managed with [uv](https://github.com/astral-sh/uv).
- `uv venv` → `uv sync` → `source .venv/bin/activate` (or prefix commands with `uv run`).
- Dependencies are always project-local — **never installed globally**.
- **Enable the git hooks** once per clone: `git config core.hooksPath .githooks`. This is shared
  config, so linked worktrees inherit it — no need to re-run it in each one.

## Running checks

| Check | Command |
| ----- | ------- |
| Tests | `uv run pytest -v` (property tests via `hypothesis`; target ≥90% coverage on the core) |
| Lint  | `uv run ruff check .` |
| Format | `uv run ruff format .` |
| Types | `uv run mypy .` |
| Imports | `uv run lint-imports` (dependency-direction boundaries, ADR-0032) |

The default paper-exchange + in-memory-bus path runs with **no external services and no API keys**.

### Test tiers

`uv run pytest` on a bare clone is **hermetic** — paper exchange + in-memory bus + SQLite, plus
`hypothesis` property tests — and needs no services or keys. The `KafkaBus` adapter is covered here
too: its unit tests run against an in-process fake broker
([`tests/_support/kafka_fakes.py`](tests/_support/kafka_fakes.py)), not a real cluster.

Two markers reach for real infrastructure and **auto-skip** when it isn't configured, so the default
run stays green without them. They differ in CI: `postgres` is hermetic (a pinned container, offline,
deterministic) and **is** in the gate; `live` reaches a real venue and never can be.

- **`postgres`** — the `PostgresStore` contract
  ([ADR-0019](docs/adr/0019-durable-store-sqlite-default-postgres-second.md)). Auto-skips unless
  `STORE_POSTGRES_DSN` points at a reachable server. Bring one up with `docker compose up -d postgres`,
  then run `STORE_POSTGRES_DSN=postgresql://tickwright:tickwright@localhost:5432/tickwright uv run
  pytest -m postgres`. `-m "not postgres"` deselects them. CI runs this tier against a `postgres:17`
  service on the `ci` job (issue #253) — locally it stays opt-in, so `uv sync && uv run pytest` needs
  no Docker.
- **`live`** — places **real orders on Hyperliquid testnet**
  ([ADR-0022](docs/adr/0022-testing-strategy.md)). Auto-skips unless you opt in with
  `TICKWRIGHT_LIVE_TESTNET=1` **and** `TICKWRIGHT_HYPERLIQUID__SIGNING_KEY` holds a funded testnet key
  — the key alone never enrolls the suite, so CI can run everything under a hostile config. Run it
  manually/nightly with `TICKWRIGHT_LIVE_TESTNET=1 uv run pytest -m live`; **never** part of the CI gate.

To exercise the real `KafkaBus` end-to-end (rather than its fake-broker unit tests), run the *app* on
Kafka, not the suite: `docker compose up -d kafka`, then `TICKWRIGHT_BUS=kafka uv run tickwright`.
[`.env.example`](.env.example) is the canonical reference for every backend's variables.

### Git hooks (local convenience, not the gate)

With `core.hooksPath` enabled (see setup):

- **pre-commit** auto-runs `ruff check --fix` + `ruff format` on your *staged* Python and re-stages
  the result (unstaged hunks are preserved safely).
- **commit-msg** runs your local hook guard if you have one (see below), and does nothing otherwise.
- **pre-push** runs `mypy` on changed Python before the push leaves your machine.

They exist to catch problems early; the **authoritative gate is CI**, which re-runs everything and
can't be skipped with `--no-verify`.

#### Local hook guards

`pre-commit` and `commit-msg` each finish by handing off to an optional **local hook guard**: an
executable you keep outside the repo, so it is never published and runs only on the machines that
have it. Nothing here installs one, and you do not need one to contribute — but if you add one,
this is the contract it is held to:

- **Location** — `hooks-local/<hook-name>` under the *common* git directory, so
  `.git/hooks-local/pre-commit` and `.git/hooks-local/commit-msg`. The hooks resolve it with
  `git rev-parse --git-common-dir` rather than `--git-dir`, which is what lets linked worktrees
  share the one install instead of quietly skipping it.
- **Arming** — the executable bit. A guard that is absent or non-executable is skipped and the
  commit proceeds. That is the normal case for nearly every clone, so it is silent by design.
- **Authority** — a guard that exits non-zero aborts the commit, and that veto is final. Any change
  to the delegation must keep a failing guard failing: this is the seam the maintainer's private
  reference scrub hangs off, backing the "no code is copied from any prior/private codebase" rule
  in [`CLAUDE.md`](CLAUDE.md). `tests/test_githooks.py` fences both halves against a real `git`.

#### Per-worktree vs shared git state

Linked worktrees share one **common** git directory — `refs/stash`, `hooks-local/`, and config all
live there and are seen identically from every worktree — but each worktree also has its **own**
private git dir (`.git/worktrees/<name>`). A hook that reads or writes git state must pick between
the two deliberately, and every hook comment that names a `--git-dir` / `--git-common-dir` /
`refs/worktree/` choice is applying this one rule:

- **Shared** state must be resolved so all worktrees agree on it. The local hook guard is looked up
  with `git rev-parse --git-common-dir` (not `--git-dir`, which is `.git/worktrees/<name>` in a
  linked worktree, where the guard would be invisible), so one install serves every worktree.
- **Per-worktree scratch** must never land on shared state, or two concurrent hooks collide and
  silently swap each other's uncommitted work. `pre-commit`'s unstaged-patch snapshot goes under
  `--git-dir` (the private dir); `pre-push`'s autostash is parked under `refs/worktree/` (git's
  per-worktree ref namespace) and stays off the shared `refs/stash` stack entirely, so a stash the
  hook created is the only thing it can restore. `tests/test_githooks.py` fences the `pre-push` case
  against a real linked worktree.

This matters most under parallel worktrees, where concurrent hooks are the norm rather than the
exception.

## How we work

- **Spec first.** Requirements are stress-tested ("grilled") into a written specification before
  any code. Load-bearing decisions are recorded as **ADRs** under [`docs/adr/`](docs/adr/), and the
  shared domain language lives in [`CONTEXT.md`](CONTEXT.md). Read these before proposing a change.
- **Vertical slices.** A change crosses every relevant layer (feed → strategy → exchange → engine)
  in a single PR, rather than delivering one horizontal layer in isolation.
- **Test-driven.** Write a failing test first, make it pass, then refactor — one behavior at a
  time. Mock only at process boundaries (HTTP/WS, Kafka, the clock, randomness); never mock the
  engine's own classes.
- **Small commits.** One logical change per commit. Match the surrounding naming and style.
- **Docs stay in sync.** If a change alters something documented elsewhere — workflow conventions
  (`docs/agents/`, `docs/workflow/`), agent skills (`.claude/skills/`), ADRs, `CONTEXT.md`, CI
  workflows — update every affected file in the same PR. Prefer one canonical source that others
  link over copies that can drift.

## Conventions

- **Branches:** `<type>/<short-slug>` — e.g. `feat/per-symbol-ordering`, `fix/ghost-reconcile-race`.
- **Commits:** [Conventional Commits](https://www.conventionalcommits.org/) — `type: imperative subject`
  (`feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `ci`). One logical change per commit.

## Pull requests

- **One PR per change — no mixed-concern PRs.** Reference the issue it closes in the body
  (`Closes #N`); don't close issues by hand — merging the PR closes them.
- Keep the diff focused and the description clear about *what* changed and *why*.
- A PR is mergeable once it passes review and the lint / type / test / coverage gate.

## Code style

- Line length **100**; Ruff formatter (double quotes); Ruff lint rules `E, W, F, I, B, C4, UP`;
  mypy with `check_untyped_defs`.
- Prefer **deep modules with small interfaces**, clear names, and comments only where they earn
  their place.

## License

By contributing, you agree that your contributions are licensed under the project's
[Apache-2.0](LICENSE) license.
