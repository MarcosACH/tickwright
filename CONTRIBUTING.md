# Contributing to Tickwright

Thanks for your interest in Tickwright — a readable, event-driven algorithmic trading engine
built as a reference implementation. **Clarity and correctness are the priorities**; please keep
changes small, well-tested, and easy to read.

## Development setup

- **Python 3.13**, managed with [uv](https://github.com/astral-sh/uv).
- `uv venv` → `uv sync` → `source .venv/bin/activate` (or prefix commands with `uv run`).
- Dependencies are always project-local — **never installed globally**.

## Running checks

| Check | Command |
| ----- | ------- |
| Tests | `uv run pytest -v` (property tests via `hypothesis`; target ≥90% coverage on the core) |
| Lint  | `uv run ruff check .` |
| Format | `uv run ruff format .` |
| Types | `uv run mypy .` |

The default paper-exchange + in-memory-bus path runs with **no external services and no API keys**.

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

## Pull requests

- Open one PR per change and reference the issue it closes in the body (`Closes #N`). Don't close
  issues by hand — merging the PR closes them.
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
