# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Tickwright** is an event-driven algorithmic trading engine, built as a rigorous, readable **reference implementation**. It turns a market feed into orders through an event-driven pipeline: `MarketFeed → Strategy → Exchange`, coordinated by an `EventBus`, with a crash-safe order-lifecycle saga, idempotent recovery, and exchange reconciliation.

v1 scope: Hyperliquid (real, read-only-auth market data) + an in-process deterministic **paper exchange**; `InMemory` and `Kafka` event-bus backends; engine-only, live/paper execution, **no backtesting**. See `README.md` for the full vision, scope, and non-goals.

## Workflow (grill-with-docs)

This repo uses the grill-with-docs / tracer-bullet workflow. Single repo; vertical-slice issues.

Pipeline:

1. `/grill-with-docs` — exhaustive Phase 0 interview to define **all** requirements and scope before any code. Resolve terms in `CONTEXT.md`; write ADRs for load-bearing decisions. **Gate:** no PRD, no code until the maintainer confirms requirements/scope are signed off.
2. `/to-spec` — synthesize the parent PRD issue (**synthesis-only, no interview** — alignment already happened in step 1). The artifact is still a PRD.
3. `/module-map` — architecture anchor at `docs/module-maps/<slug>.md`.
4. `/to-tickets` — break the PRD into vertical-slice child tickets, linked as GitHub sub-issues (all in this repo), each declaring its blocking edges. The last child is always an `integrate-and-verify` slice, blocked by all its siblings, asserting the cross-slice scenarios no single slice could (ADR-0050).
5. `/tdd` per child — red-green-refactor on `ralph/issue-<N>` branch; vertical tracer through `feed → strategy → exchange → engine`.
6. Open one PR per issue with `Closes #<N>` in the body.
7. `/code-review` — structured BLOCKING/WARN/NIT review; label `ralph:ready` when clean. Merge closes the issue automatically.

Auxiliary skills (used as needed, not part of the linear pipeline): `/wayfinder` — chart a huge, foggy effort as a map of decision tickets on the tracker before it's spec-able; `/research` — delegate primary-source investigation to a background agent, cited Markdown under `docs/research/`; `/prototype` — throwaway logic prototype to pressure-test a state model before committing.

See `docs/workflow/labels.md` for the label schema and `docs/agents/issue-tracker.md` for gh CLI / project-board conventions.

## Required Behavior

- **Be concise.** Keep responses short and to the point; skip preamble, hedging, and restating the question.
- Always aim for best software-engineering and architectural practices.
- Prefer minimal, targeted edits over broad rewrites; match existing naming and style.
- **No code is copied from any prior/private codebase.** Generalizable patterns are reimplemented from first principles and validated against current best practice before coding.
- **TDD policy (mandatory):** red test first, green implementation, refactor only after green. One behavior at a time.
- **Vertical slice policy (mandatory):** features/bugfixes cross every relevant layer (feed → strategy → exchange → engine) in one PR. Never deliver a horizontal layer in isolation.
- **PR policy (mandatory):** all code changes ship as PRs with `Closes #N` in the body, targeting **`main`** — there is no long-lived PRD or integration branch, because GitHub honours a closing keyword only against the default branch and `pr-policy` would pass anyway, reporting green with no effect (`docs/adr/0050-trunk-based-delivery-and-prd-level-verification.md`). Never close issues manually — the PR merge closes them. The exceptions are the issues with no merge event of their own: a parent PRD, closed deliberately at release (`docs/workflow/versioning.md`), and a wayfinder ticket, closed on resolution, with its map closed on hand-off (`docs/agents/issue-tracker.md` → *Wayfinding operations*).
- **Release policy (mandatory):** when work reaches a shippable milestone (a PRD delivered, a meaningful feature set, an important fix), proactively propose a release — a version number *with its SemVer rationale* — and wait for maintainer sign-off before tagging. Never self-authorize a tag or GitHub Release. Full policy and procedure: `docs/workflow/versioning.md`.
- **Commit grouping:** one logical change per commit (not one file per commit). Group tightly-related changes; split unrelated ones.
- **Two implementations per seam, no more.** One looks hardcoded; three is scope creep.
- **Docs-sync policy (mandatory):** a change that alters anything documented elsewhere — workflow conventions (`docs/agents/`, `docs/workflow/`), skills (`.claude/skills/`), invariants (`docs/agents/invariants.md`), ADRs, `CONTEXT.md`, CI workflows, or this file — updates every affected file **in the same PR**. Duplication that can drift is a bug: prefer linking one canonical source over copying, and when two files disagree, fix the copy, not the canon.
- **Dependencies are managed with `uv` in a project virtual environment — never installed globally.**

## Project Context Files

- Domain glossary: `CONTEXT.md`
- Architecture decisions: `docs/adr/`
- Module maps for in-flight features: `docs/module-maps/`
- Primary-source research notes: `docs/research/` — dated captures backing the ADRs, **not maintained**; where a note and an ADR disagree, the ADR wins
- Workflow & label schema: `docs/workflow/labels.md`
- Versioning, tags & releases: `docs/workflow/versioning.md`
- Issue-tracker conventions: `docs/agents/issue-tracker.md`
- Triage label roles: `docs/agents/triage-labels.md`
- Skill evals (the tier that tests `.claude/skills/`, not the engine): `evals/README.md`

## Context Discipline

- Never read `.venv/`, `__pycache__/`, `.mypy_cache/`, `.pytest_cache/`, `.ruff_cache/`, `.import_linter_cache/`, `*.egg-info/`, `*.pyc`, `logs/`, `*.log`. The `.claude/settings.json` deny list enforces this.
- For `CONTEXT.md`, ADRs, `docs/module-maps/*.md`, and `docs/research/*.md`, use `.agents/tools/doc-slice`:
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

# infrastructure for the non-default backends (optional; the hermetic
# in-memory-bus + SQLite path needs nothing)
docker compose up -d postgres   # Postgres for the PostgresStore path (ADR-0019)
docker compose up -d kafka      # Kafka broker for the KafkaBus path (ADR-0028)
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
- The suite is **hermetic against ambient config**: an outcome must never depend on a developer `.env` or an exported `TICKWRIGHT_*` var. Build the pure `AppConfig`, never `AppSettings` (see *Environment Variables*), and hand any subprocess a `TICKWRIGHT_`-scrubbed environment. CI asserts this by running `tests/app` under a hostile live-venue env.
- Tests marked `postgres` need a real Postgres (`PostgresStore` contract, ADR-0019); they auto-skip unless `STORE_POSTGRES_DSN` points at a reachable server. Bring one up with `docker compose up -d postgres`, then `STORE_POSTGRES_DSN=postgresql://tickwright:tickwright@localhost:5432/tickwright uv run pytest -m postgres`. `-m "not postgres"` deselects them. **CI runs this tier** — the `ci` job starts a `postgres:17` service and sets the DSN (issue #253), so the ADR-0019 parity promise is gated, not merely available; the local default still auto-skips. **The DSN must address a database dedicated to the suite** — the per-test reset truncates every table in the schema, reading the list from `pg_tables` rather than a transcribed one, so it owns whatever it finds there.
- Tests marked `live` place real orders on Hyperliquid **testnet** (ADR-0022); they auto-skip unless you opt in with `TICKWRIGHT_LIVE_TESTNET=1` **and** `TICKWRIGHT_HYPERLIQUID__SIGNING_KEY` holds a funded testnet key. The opt-in flag is a dedicated run-gate mapping onto no config field (issue #73), so the key alone never enrols the suite and CI can run the whole thing under a hostile config. Run locally with `TICKWRIGHT_LIVE_TESTNET=1 uv run pytest -m live`; in CI it runs from `.github/workflows/ci-live.yml` — **weekly** (Mondays 10:17 UTC) plus `workflow_dispatch`, on the repo's `TICKWRIGHT_HYPERLIQUID__SIGNING_KEY` secret and `TICKWRIGHT_HYPERLIQUID__ACCOUNT_ADDRESS` variable (issue #255). Never part of the merge gate and never a required check. Weekly is the pre-1.0 setting — the tier catches venue drift, which accrues by calendar rather than by commit; escalate to nightly when real money is in play. Note it never calls `start()`, so ADR-0046's account-mode boot gate is not covered live.

## Skill Evals

A separate tier from `pytest`: it tests the skills in `.claude/skills/`, not the engine. Run with `claude plugin eval .` from the repo root; cases live in `evals/**/case.yaml`, one behavior each, and every case runs a with-skill and a without-skill arm so a case that scores the same in both is exposed as testing the base model rather than the skill. Never a merge gate — scores are noisy and the runner reaches the API. Run it when you edit a skill, and before a release. The runner is early access and not enabled here yet, so the committed cases have not been run. Full conventions, grader reference and cost discipline: `evals/README.md`.

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

The CLI (`tickwright` / `python -m tickwright.app`) reads `AppSettings` from the environment and `.env`. **`.env.example` is the canonical variable reference** — every variable maps onto a field of `AppConfig` (`src/tickwright/app/config.py`) with the `TICKWRIGHT_` prefix, `__` for nesting, and JSON for complex values (e.g. `TICKWRIGHT_REPLAY__PATH`, `TICKWRIGHT_STRATEGIES`).

`config.py` holds two classes and the split is load-bearing (issue #71): `AppConfig` is a pure `BaseModel` that reads nothing ambient and is what `build_engine` takes and tests build; `AppSettings` subclasses it with the env/`.env` skin, and **`__main__.py` is its only legitimate builder**. Reading ambient config anywhere else — including a test — lets a developer `.env` or an exported `TICKWRIGHT_*` var outrank the class defaults and silently wire a live venue into a paper path. `AppSettings` stays out of `app`'s `__all__` for that reason; don't export it.

The `KafkaBus` backend reads `TICKWRIGHT_BUS=kafka` plus `TICKWRIGHT_KAFKA__{BOOTSTRAP_SERVERS,EVENTS_TOPIC,GROUP_ID}` (ADR-0028; the `docker compose up kafka` service advertises the default `localhost:9092`). The `PostgresStore` backend reads `TICKWRIGHT_STORE=postgres` plus `TICKWRIGHT_POSTGRES__DSN` (ADR-0019; the `docker compose up postgres` service is its default). The live `HyperliquidFeed` reads `TICKWRIGHT_FEED=hyperliquid` plus `TICKWRIGHT_HYPERLIQUID__{SYMBOLS,TESTNET,...}` (ADR-0021; no API key — the trades channel is unauthenticated). The live `HyperliquidExchange` reads `TICKWRIGHT_EXCHANGE=hyperliquid` plus `TICKWRIGHT_HYPERLIQUID__{SIGNING_KEY,ACCOUNT_ADDRESS,SLIPPAGE_BOUND}` (ADR-0030; the signing key is env-only, never persisted, redacted from logs — the paper default needs none). The default `PaperExchange` needs no key and no service. ADR-0042's two paper-account genesis variables are **wired** (issue #171): `TICKWRIGHT_PAPER__GENESIS_COLLATERAL` (> 0, no default: the account's opening cash line, so there is nothing sane to assume — demanded by an `AppConfig` model validator only when `TICKWRIGHT_EXCHANGE=paper`, never required at field level, so a live run is never asked for it) and `TICKWRIGHT_PAPER__ACCOUNT_LABEL` (defaults to `default`, a lowercase slug with no hyphen). Both reach the engine through `PaperExchange.account_spec()`, whose `paper-<label>` id is deliberately two segments against live's `hyperliquid-<network>-<address>` three. Both are now **persisted**: `PortfolioProjection.recover()` seeds the paper account row from that same spec on a first start against an empty store, and restores it rather than re-seeding on every start after (ADR-0043 §6, issue #187). Their fail-fast when the configured values disagree with the row already there is **wired** (issue #188): a changed genesis or a changed label is refused at startup with `StoreAccountMismatch`, raised from `recover()`'s check step ahead of `cache.rebuild()` and naming **every** disagreeing field at once rather than one per restart. The same error refuses a paper store carrying order history with no ledger behind it — fees that were never charged and funding that never existed must not be backfilled as zeroes — asked with `Store.has_orders()` rather than the mass read. Both conditions are gated on `account_spec().genesis_collateral is not None`, the declared-versus-ingested predicate: on live the genesis has no configured counterpart to disagree with, and a store predating the ledger is legitimate and heals from the venue. `account_id` compares on both paths. ADR-0043 fixes the ledger schema those variables are checked against — the genesis column is `NOT NULL` with no `CHECK`, and `AccountSpec.genesis_collateral` is what carries the configured value to the startup check. ADR-0040/0044 add a third **specified-but-not-yet-wired** variable, `TICKWRIGHT_LEVERAGE` (JSON, symbol → `{mode, leverage}`; default `1x`/`isolated` per symbol) — deliberately **venue-agnostic**, not nested under `PAPER__` or `HYPERLIQUID__`, because the model reading it is venue-agnostic and no live run may read a paper block (ADR-0042 §1). It is validated against `InstrumentSpec.max_leverage` on **both** paths at startup (leaving live's half to the venue would let paper compute off a leverage live rejects, surfacing only on promotion); on live it is *additionally* pushed to the venue once at startup via `updateLeverage`, refusing to start rather than re-margining a symbol that already holds a position at a different setting. Paper validates and never writes.

ADR-0046 adds a live-only **precondition with no variable**: the Hyperliquid account must be in **Manual/Standard** account-abstraction mode (`userAbstraction` reading `default` or `disabled`). Under `unifiedAccount` or `portfolioMargin` the perps clearinghouse reports only the collateral posted into perps, so account equity and free margin read an order of magnitude low with nothing in the response indicating it — so the mode is **read from the venue and verified at boot** (ahead of ADR-0044's leverage push, gating it — ADR-0024 step 4 opens with it) and re-read before any Tier-1 cash heal. The guard fails closed at both points: boot refuses to start on an unsupported *or unreadable* mode (`VenueAccountModeUnsupported`), and in flight a changed *or unverifiable* mode refuses the heal and freezes the account-grain reconcile (`ACCOUNT_MODE_UNVERIFIED`). Switching an account is a **user-signed** action an agent wallet cannot perform: `userSetAbstraction("disabled")` with the master wallet, then a spot→perps `usdClassTransfer`. Nothing is configured, so `.env.example` gains nothing.
