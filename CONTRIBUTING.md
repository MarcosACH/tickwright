# Contributing to Tickwright

Tickwright is a trading engine that can move real money. Correctness comes first. Keep changes
small, tested, and easy to read.

Before you start, read [`CONTEXT.md`](CONTEXT.md) for the vocabulary and skim
[`docs/adr/`](docs/adr/) for the decisions already made.

## Setup

Python 3.13, managed with [uv](https://github.com/astral-sh/uv). Dependencies live in the project
`.venv`. Never install them globally.

```bash
uv venv && uv sync
git config core.hooksPath .githooks   # once per clone
```

The git hooks run ruff on commit and mypy on push. They are a convenience. CI is the gate.

If you use Claude Code, `.claude/hooks/` enforces the rules in [`CLAUDE.md`](CLAUDE.md) at each
tool call. How they work is in [`docs/agents/hooks.md`](docs/agents/hooks.md).

## Checks

| Check | Command |
| ----- | ------- |
| Tests | `uv run pytest -v` |
| Lint | `uv run ruff check .` |
| Format | `uv run ruff format .` |
| Types | `uv run mypy` (no `.`, it would skip `.claude/hooks`) |
| Imports | `uv run lint-imports` (dependency direction, ADR-0032) |

CI runs all five and fails under 90% coverage.

## Test tiers

The default run is hermetic. It uses the paper exchange, the in-memory bus, SQLite, and an
in-process fake Kafka broker. It needs no service, no API key, and no `.env`.

Two markers reach real infrastructure. Both skip themselves when it is not configured.

- `postgres` runs the `PostgresStore` contract against a real server. Start one with
  `docker compose up -d postgres`, then run
  `STORE_POSTGRES_DSN=postgresql://tickwright:tickwright@localhost:5432/tickwright uv run pytest -m postgres`.
  CI runs this tier.
- `live` places real orders on Hyperliquid testnet. It needs `TICKWRIGHT_LIVE_TESTNET=1` and a
  funded key in `TICKWRIGHT_HYPERLIQUID__SIGNING_KEY`. Run it with
  `TICKWRIGHT_LIVE_TESTNET=1 uv run pytest -m live`. CI runs it weekly, never on a PR.

To try the real Kafka bus, run the app rather than the tests: `docker compose up -d kafka`, then
`TICKWRIGHT_BUS=kafka uv run tickwright`. [`.env.example`](.env.example) lists every variable.

Skill evals in [`evals/`](evals/README.md) test the agent skills, not the engine. You only need
them when you change a skill.

## How we work

- **Spec first.** Requirements are written down before code. Decisions go in an ADR.
- **Test first.** Write a failing test, make it pass, then refactor. One behavior at a time.
- **Mock at process boundaries only.** HTTP, WebSocket, the Kafka client, the clock, randomness.
  Never mock our own classes.
- **Stay hermetic.** A test must not depend on a developer `.env` or an exported `TICKWRIGHT_*`
  variable. Build `AppConfig` directly, never `AppSettings`.
- **Vertical slices.** A change crosses every layer it touches in one PR.
- **Docs stay in sync.** If a change makes another file wrong, fix it in the same PR.
- **Closed lists get a coverage test.** The seams, the config backends, and the event catalog are
  fixed lists. Each has a test that every member is exercised. See
  [`tests/_support/closed_sets.py`](tests/_support/closed_sets.py).

## Branches, commits, and PRs

- Every change starts as a GitHub issue. Work on `ralph/issue-<N>`.
- Commits follow [Conventional Commits](https://www.conventionalcommits.org/):
  `type(scope): subject`, with type one of `feat`, `fix`, `docs`, `refactor`, `test`, `chore`,
  `ci`. One logical change per commit.
- One PR per issue, targeting `main`, with `Closes #<N>` in the body. Merging closes the issue.
  Never close it by hand.
- Open with the problem, then the solution. Keep the diff focused.

## Code style

Line length 100. Ruff formatter, double quotes. Ruff rules `E, W, F, I, B, C4, UP`. Mypy with
`check_untyped_defs`. Comments say why, not what.

## License

By contributing, you agree that your contributions are licensed under
[Apache-2.0](LICENSE).
