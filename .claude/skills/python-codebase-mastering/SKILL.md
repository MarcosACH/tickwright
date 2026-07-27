---
name: python-codebase-mastering
description: Python-focused codebase mastering pass for high-impact refactoring, package/module/folder structure, public API shape, testability, and release-readiness polish. Use when the user asks to "master" a Python codebase, harden it before release, improve folder structure, clean up architecture, or perform scoped refactoring without changing behavior.
---

# Python Codebase Mastering

Goal: make an existing Python codebase easier to ship, test, navigate, and maintain without changing behavior.

## Non-Goals

- New features.
- Broad rewrites.
- Style churn.
- Framework swaps.
- Refactors not tied to a concrete maintenance, testing, reliability, or release-readiness payoff.

If the code needs a deep redesign before it can be mastered, say so and move the work back to architecture/design instead of hiding risk inside "cleanup."

## Operating Rule

Lock behavior first. Refactor only while tests are green or after adding a focused characterization test for the behavior being preserved.

One refactor slice at a time:

1. Identify friction.
2. State intended behavior preservation.
3. Make the smallest structural change that removes the friction.
4. Run the narrowest useful `pytest` scope.
5. Continue only after green.

## Orientation

Before changing code:

- Read `CONTEXT.md` (root) and any per-module `CONTEXT.md` for domain vocabulary.
- Check relevant ADRs in `docs/adr/`.
- Use `.agents/tools/doc-slice` for `docs/module-maps/*.md` and any large doc.
- Inspect module trees with `git ls-files` or `rg --files`, not broad directory dumps.
- Ignore `.venv/`, `__pycache__/`, `.mypy_cache/`, `.pytest_cache/`, `.ruff_cache/`, `logs/`, `*.egg-info/`, `*.pyc`.

Restate the work as a verifiable goal:

- `preserve behavior -> simplify module boundary -> targeted pytest green`
- `preserve behavior -> move code to domain-owned module -> targeted pytest green`
- `preserve behavior -> narrow public API -> targeted pytest green`

## What To Look For

High-impact mastering candidates:

- Shallow modules whose interface is nearly as complex as their implementation.
- Cross-package calls that force callers to understand private workflow steps (e.g. importing from a leaf module deep inside `tickwright.strategy.*`).
- Domain logic split across folders by technical layer instead of domain concept.
- Public items exposed only because folder structure made privacy inconvenient — symbols not in any `__all__` but imported from across the package.
- Duplicated validation, parsing, conversion, or error mapping (e.g. quantity quantization repeated in the exchange adapter and the strategy).
- Primitive-heavy signatures where a small type would make invalid states unrepresentable (e.g. an `order_side: str` that should be a `Literal`/`Enum`, or a raw `int` venue id that should be a `NewType`).
- Long `if/elif` chains on a string discriminator where a dispatch dict or polymorphism would simplify.
- `main.py` / entrypoint scripts containing business logic instead of wiring.
- Tests coupled to private implementation, brittle import paths, or incidental helpers.
- Modules named after actions like `utils`, `helpers`, `manager`, `processor`, `common`, unless the name carries real domain meaning.
- Mutable module-level state mutated at import time or by hot paths — hard to test, hard to reason about under asyncio.

Folder-structure signals:

- Each package's `__init__.py` should be a narrow public facade with an explicit `__all__`, not the implementation dumping ground.
- Entrypoint scripts should parse/configure/wire, then call library code.
- Package folders should reflect domain ownership and runtime boundaries (market feed vs strategy vs exchange adapter vs saga vs event bus vs reconciliation).
- Keep module names aligned with `CONTEXT.md` terms.
- Prefer privacy over convention: prefix internals with `_`, export only what callers need.
- Follow existing layout (per-feature subpackage vs single module) unless the refactor is explicitly about layout.
- Colocate tests with the package when existing style does; use integration tests when testing public package behavior.

Python-specific quality checks:

- Prefer owned domain types over loosely-related primitives when it reduces caller knowledge — use `NewType`, frozen `dataclass`, or `pydantic.BaseModel` for value objects.
- Use `__init_subclass__`, `Protocol`, or `ABC` to make interfaces explicit at boundaries.
- Avoid silent fallback conversions (`int(some_str)` without a try/except contract; `float(decimal)` in money paths).
- Keep error messages actionable and include causative values at boundaries (but never log secrets).
- Do not add production `# noqa` / `# type: ignore` for avoidable design issues. If the lint rule is wrong for one line, justify it inline.
- Remove orphaned imports/items caused by the refactor (ruff `F401` will catch these).

## Candidate Format

When presenting options, keep it short:

| Candidate | Files | Change | Payoff | Risk | Verify |
|---|---|---|---|---|---|

Rank by:

1. Release risk reduced.
2. Testability gained.
3. Public API narrowed.
4. Folder/module navigation improved.
5. Lines deleted.

Do not propose more than 5 candidates at once.

## Execution Rules

- Keep behavior stable.
- Keep diffs surgical.
- Do not rename public APIs unless the payoff is concrete and tests/import errors can chase callers safely.
- Do not move files just for aesthetic symmetry.
- Delete pass-through wrappers only when callers get simpler.
- Merge modules when the split adds navigation cost without isolation.
- Split modules when one file owns multiple domain concepts or test surfaces.
- Update `CONTEXT.md` only when the refactor clarifies or introduces domain language.
- Update ADRs only when preserving a decision or recording a structural choice that future agents must respect.

Ask before proceeding when:

- The change alters a public symbol imported by another package in the codebase.
- The change crosses package boundaries.
- The change reopens an ADR.
- The behavior lock is missing and characterization tests would be large.

## Verification

Use the narrowest meaningful checks while iterating:

- `uv run ruff format .`
- `uv run ruff check .`
- `uv run mypy <module>` for the touched module
- `uv run pytest <module>/tests -v`
- Broaden to the full suite only when the refactor crosses packages.

For this repo, open and respect the canonical invariants in `docs/agents/invariants.md` — the single source, never copied here, since a copy that falls behind it silently narrows the behavior lock. Each entry cites its ADR; read the ADR before touching that area. A refactor that regresses one of them is not a mastering pass — it is a behavior change and goes back to design.

Done means:

- Targeted tests pass.
- Public behavior is preserved or intentionally documented.
- Module/folder ownership is clearer.
- New names match domain vocabulary in `CONTEXT.md`.
- Final response lists changed files, verification run, and residual risk.
