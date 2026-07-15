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

### Git hooks (local convenience, not the gate)

With `core.hooksPath` enabled (see setup):

- **pre-commit** auto-runs `ruff check --fix` + `ruff format` on your *staged* Python and re-stages
  the result (unstaged hunks are preserved safely).
- **pre-push** runs `mypy` on changed Python before the push leaves your machine.

They exist to catch problems early; the **authoritative gate is CI**, which re-runs everything and
can't be skipped with `--no-verify`.

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
