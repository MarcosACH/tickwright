# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Tickwright** is an event-driven algorithmic trading engine, built as a rigorous, readable **reference implementation**. It turns a market feed into orders through an event-driven pipeline: `MarketFeed → Strategy → Exchange`, coordinated by an `EventBus`, with a crash-safe order-lifecycle saga, idempotent recovery, and exchange reconciliation.

v1 scope: Hyperliquid (real, read-only-auth market data) + an in-process deterministic **paper exchange**; `InMemory` and `Kafka` event-bus backends; engine-only, live/paper execution, **no backtesting**. See `README.md` for the full vision, scope, and non-goals.

## Workflow (grill-with-docs)

This repo uses the grill-with-docs / tracer-bullet workflow (Matt Pocock). Single repo; vertical-slice issues.

Pipeline:

1. `/grill-with-docs` — exhaustive Phase 0 interview to define **all** requirements and scope before any code. Resolve terms in `CONTEXT.md`; write ADRs for load-bearing decisions. **Gate:** no PRD, no code until requirements/scope are signed off.
2. `/to-prd` — parent PRD issue on the GitHub project.
3. `/module-map` — architecture anchor at `docs/module-maps/<slug>.md`.
4. `/to-issues` — break the PRD into vertical-slice child issues, linked as GitHub sub-issues (all in this repo).
5. `/tdd` per child — red-green-refactor on `ralph/issue-<N>` branch; vertical tracer through `feed → strategy → exchange → engine`.
6. Open one PR per issue with `Closes #<N>` in the body.
7. `/code-review` — structured BLOCKING/WARN/NIT review; label `ralph:ready` when clean. Merge closes the issue automatically.

See `docs/workflow/labels.md` for the label schema and `docs/agents/issue-tracker.md` for gh CLI / project-board conventions.

## Required Behavior

- Always aim for best software-engineering and architectural practices.
- Prefer minimal, targeted edits over broad rewrites; match existing naming and style.
- **No code is copied from any prior/private codebase.** Generalizable patterns are reimplemented from first principles and validated against current best practice before coding.
- **TDD policy (mandatory):** red test first, green implementation, refactor only after green. One behavior at a time.
- **Vertical slice policy (mandatory):** features/bugfixes cross every relevant layer (feed → strategy → exchange → engine) in one PR. Never deliver a horizontal layer in isolation.
- **PR policy (mandatory):** all code changes ship as PRs with `Closes #N` in the body. Never close issues manually — the PR merge closes them.
- **Commit grouping:** one logical change per commit (not one file per commit). Group tightly-related changes; split unrelated ones.
- **Two implementations per seam, no more.** One looks hardcoded; three is scope creep.
- **Docs-sync policy (mandatory):** a change that alters anything documented elsewhere — workflow conventions (`docs/agents/`, `docs/workflow/`), skills (`.claude/skills/`), invariants (`docs/agents/invariants.md`), ADRs, `CONTEXT.md`, CI workflows, or this file — updates every affected file **in the same PR**. Duplication that can drift is a bug: prefer linking one canonical source over copying, and when two files disagree, fix the copy, not the canon.
- **Dependencies are managed with `uv` in a project virtual environment — never installed globally.**

## Project Context Files

- Domain glossary: `CONTEXT.md`
- Architecture decisions: `docs/adr/`
- Module maps for in-flight features: `docs/module-maps/`
- Workflow & label schema: `docs/workflow/labels.md`
- Issue-tracker conventions: `docs/agents/issue-tracker.md`
- Triage label roles: `docs/agents/triage-labels.md`

## Context Discipline

- Never read `.venv/`, `__pycache__/`, `.mypy_cache/`, `.pytest_cache/`, `.ruff_cache/`, `.import_linter_cache/`, `*.egg-info/`, `*.pyc`, `logs/`, `*.log`. The `.claude/settings.json` deny list enforces this.
- For `CONTEXT.md`, ADRs, and `docs/module-maps/*.md`, use `.agents/tools/doc-slice`:
  - `.agents/tools/doc-slice <file>` — list TOC (`line  level  heading`)
  - `.agents/tools/doc-slice <file> <heading-substr>` — print just that section
- For large source/test files, use `Read` with `offset`/`limit` targeting the symbol you need.
- For GitHub issues, use `gh issue view <N>`.

## Local Development (macOS)

Package/dependency manager is **[uv](https://github.com/astral-sh/uv)** against a project `.venv`. Dependencies are never installed globally.

```bash
# one-time
uv venv                 # create .venv
uv sync                 # install locked deps
source .venv/bin/activate   # optional; or prefix commands with `uv run`

# infrastructure for the Kafka bus path (optional; the in-memory bus needs nothing)
# docker compose up -d        # (compose file added during implementation)
```

## Tests

Run with the project venv via `uv run`:

| Scope        | Command                                              |
| ------------ | ---------------------------------------------------- |
| All          | `uv run pytest -v`                                   |
| One dir      | `uv run pytest tests/<area> -v`                      |
| Single test  | `uv run pytest tests/test_x.py::TestClass::test -v`  |

- `hypothesis` for property-based tests. Target **≥90% coverage** on the core.
- Mock at process boundaries only (HTTP/WS, Kafka client, system clock, randomness). Never mock our own classes.
- The default paper-exchange + in-memory-bus path runs with **no external services and no API keys**.

## Linting & formatting

```bash
uv run ruff check .
uv run ruff format .
uv run mypy .
uv run lint-imports   # dependency-direction boundaries (ADR-0032)
```

## Code Style

- **Line length:** 100 characters
- **Formatter:** Ruff (double quotes)
- **Linter:** Ruff rules E, W, F, I, B, C4, UP (E501 and F403 ignored)
- **Type checker:** MyPy with `check_untyped_defs = true`

## Environment Variables

The CLI (`tickwright` / `python -m tickwright.app`) reads `AppConfig` from the environment and `.env`. **`.env.example` is the canonical variable reference** — every variable maps onto `AppConfig` (`src/tickwright/app/config.py`) with the `TICKWRIGHT_` prefix, `__` for nesting, and JSON for complex values (e.g. `TICKWRIGHT_REPLAY__PATH`, `TICKWRIGHT_STRATEGIES`).

Later slices add their variables with their impls (e.g. `HYPERLIQUID_TESTNET` for the live exchange path; `KAFKA_BOOTSTRAP_SERVERS`, `KAFKA_*_TOPIC` for the `KafkaBus` backend).
