---
name: tdd
description: Test-driven development with red-green-refactor loop. Use when user wants to build features or fix bugs using TDD, mentions "red-green-refactor", wants integration tests, or asks for test-first development.
---

# Test-Driven Development

## Philosophy

**Core principle**: Tests should verify behavior through public interfaces, not implementation details. Code can change entirely; tests shouldn't.

**Good tests** are integration-style: they exercise real code paths through public APIs. They describe _what_ the system does, not _how_ it does it. A good test reads like a specification - "user can checkout with valid cart" tells you exactly what capability exists. These tests survive refactors because they don't care about internal structure.

**Bad tests** are coupled to implementation. They mock internal collaborators, test private methods, or verify through external means (like querying a database directly instead of using the interface). The warning sign: your test breaks when you refactor, but behavior hasn't changed. If you rename an internal function and tests fail, those tests were testing implementation, not behavior.

See [tests.md](tests.md) for examples and [mocking.md](mocking.md) for mocking guidelines.

## Seams — where tests go

A **seam** is the public boundary you test at: the interface where you observe behavior without reaching inside. In this repo the seams are the ADR-0032 Protocols — `Strategy`, `MarketFeed`, `Exchange`, `EventBus`, `Store`, `Clock`, fill models — wired with their real lightweight implementations (`InMemoryBus`, `SQLiteStore(":memory:")`, `PaperExchange`, `ManualClock`, `ReplayFeed`). Tests live at seams, never against internals.

**Test only at pre-agreed seams.** Before writing any test, write the seams under test down and confirm them with the user — no test is written at an unconfirmed seam. If the driving PRD already sketched its test seams (`/to-spec` does this), start from those and confirm they still hold. Agreeing the seams up front is how testing effort lands on the critical paths and complex logic instead of every edge case — prefer existing seams, and put any new one at the highest point you can.

Ask: "What's the public interface, and which seams should we test?"

## Anti-Pattern: Horizontal Slices

**DO NOT write all tests first, then all implementation.** This is "horizontal slicing" - treating RED as "write all tests" and GREEN as "write all code."

This produces **crap tests**:

- Tests written in bulk test _imagined_ behavior, not _actual_ behavior
- You end up testing the _shape_ of things (data structures, function signatures) rather than user-facing behavior
- Tests become insensitive to real changes - they pass when behavior breaks, fail when behavior is fine
- You outrun your headlights, committing to test structure before understanding the implementation

**Correct approach**: Vertical slices via tracer bullets. One test → one implementation → repeat. Each test responds to what you learned from the previous cycle. Because you just wrote the code, you know exactly what behavior matters and how to verify it.

```
WRONG (horizontal):
  RED:   test1, test2, test3, test4, test5
  GREEN: impl1, impl2, impl3, impl4, impl5

RIGHT (vertical):
  RED→GREEN: test1→impl1
  RED→GREEN: test2→impl2
  RED→GREEN: test3→impl3
  ...
```

## Anti-Pattern: Tautological Tests

A **tautological test** recomputes its expected value the way the code does, so it passes by construction and can never disagree with the code. `assert fill_price(tick) == tick.price` when the implementation *is* `return tick.price`; a property test whose oracle is a copy of the function under test; a hand-derived snapshot computed with the same formula. These are green the moment they're written and stay green when the behavior breaks — worse than no test, because they read as coverage.

Expected values must come from an **independent source of truth**: a known-good literal (`Decimal("50000")`), a worked example from the SPEC or an ADR, or a genuinely different computation. For a `hypothesis` property, assert an *invariant* the code doesn't compute the same way (duplicate-delivery convergence, a saga never leaving a legal state) — not the function's own output re-derived. See [tests.md](tests.md).

## Workflow

### 1. Planning

**If invoked with an issue reference (URL or `#N`)**, before exploring code:

- Fetch the issue: `gh issue view <n> --comments`
- Apply the "picking up an issue" transition from [docs/agents/issue-tracker.md](../../../docs/agents/issue-tracker.md): remove the `needs-triage` label if present, move project Status to `In Progress`. Do this *now*, not at PR time — it signals the issue is being worked.
- **Confirm the base branch with the user before creating `ralph/issue-<N>`.** Never silently branch off whatever happens to be checked out. Ask explicitly which branch this work should be based on — examples: `main`, an in-flight epic branch like `feature/<epic-slug>`, or a parent slice's branch. Show the candidates you can see (`git branch --show-current`, the issue's parent PRD, sibling slices' branches via recent merges) and ask. Only after the user confirms, create the branch:
  - **Default — `git checkout -b ralph/issue-<N> <base>`.** Right for the common case: a clean tree with one issue in flight. It mutates the single working tree, which is fine when nothing is in the way.
  - **Worktree — when the tree is dirty at pickup, or a second issue is genuinely in flight** (e.g. a slice and its follow-up, each wanting its own checkout and its own `.venv`). A worktree gives the slice its own checkout so nothing gets stashed and the two don't take turns on one directory:
    ```bash
    git worktree add ../tickwright-issue-<N> -b ralph/issue-<N> <base>
    cd ../tickwright-issue-<N>
    uv venv && uv sync        # a worktree inherits no untracked files, so it has no .venv; skip this and the
                              # first `uv run pytest` dies on a missing interpreter — exactly when the red test
                              # is supposed to be failing for its own reason, the worst moment to be wrong about why
    cp ../tickwright/.env .   # untracked too, so it doesn't come across either (skip if the source has none)
    ```
    Place it **outside** the repo (`../tickwright-issue-<N>`); nested, it gets swept into `ruff check .` and pytest collection. This is guidance, not a mandate — `git checkout -b` stays the default, and a worktree costs a full `uv sync` that is not worth it to fix a typo.

When exploring the codebase, use the project's domain glossary so that test names and interface vocabulary match the project's language, and respect ADRs in the area you're touching.

Before writing any code:

- [ ] Confirm with user what interface changes are needed
- [ ] **Confirm the seams under test up front** (see [Seams](#seams--where-tests-go)) — no test at an unconfirmed seam
- [ ] Confirm with user which behaviors to test (prioritize)
- [ ] Identify opportunities for [deep modules](deep-modules.md) (small interface, deep implementation)
- [ ] Design interfaces for [testability](interface-design.md)
- [ ] List the behaviors to test (not implementation steps)
- [ ] Get user approval on the plan

Ask: "What should the public interface look like? Which behaviors are most important to test?"

**You can't test everything.** Confirm with the user exactly which behaviors matter most. Focus testing effort on critical paths and complex logic, not every possible edge case.

### 2. Tracer Bullet

Write ONE test that confirms ONE thing about the system:

```
RED:   Write test for first behavior → test fails
GREEN: Write minimal code to pass → test passes
```

This is your tracer bullet - proves the path works end-to-end.

### 3. Incremental Loop

For each remaining behavior:

```
RED:   Write next test → fails
GREEN: Minimal code to pass → passes
```

Rules:

- One test at a time
- Only enough code to pass current test
- Don't anticipate future tests
- Keep tests focused on observable behavior
- Commit subjects follow Conventional Commits — `feat|fix|docs|refactor|test|chore|ci(scope)?: ...` (see `CONTRIBUTING.md`). The `pr-policy` CI check rejects the eventual PR on a single non-conforming subject, so get it right per commit, not at PR time.

### Feedback cadence

Match the feedback loop to its cost — cheap checks often, the expensive one once:

- **Typecheck regularly** — `uv run mypy .` after each green (and `uv run ruff check .` for lint). Fast, and catches the class of error a single test won't.
- **Run the single test file regularly** — `uv run pytest tests/<area>/test_x.py -v` (or `::TestClass::test`) is your inner loop; run it every red→green, not the whole suite.
- **Run the full suite once at the end** — `uv run pytest -v` before you consider the branch done, to catch cross-module regressions the focused runs miss. Don't pay for the full suite on every cycle.

### 4. Refactor

After all tests pass, look for [refactor candidates](refactoring.md):

- [ ] Extract duplication
- [ ] Deepen modules (move complexity behind simple interfaces)
- [ ] Apply SOLID principles where natural
- [ ] Consider what new code reveals about existing code
- [ ] Run tests after each refactor step

**Never refactor while RED.** Get to GREEN first.

## Scope: TDD ends at green commits

This skill stops after tests are green, code is committed, and the branch is pushed.

- **DO** move the issue's project status to `In Progress` at pickup (already covered in step 1 of the Workflow above).
- **DO NOT** open a pull request from this skill.
- **DO NOT** move the issue's project status to `In Review`.

PR creation and the `In Review` transition both belong to `/improve-codebase-architecture` — it owns the post-TDD deepening review and the PR opening step. Leave the branch pushed; the next skill takes it from there.

## Checklist Per Cycle

```
[ ] Test describes behavior, not implementation
[ ] Test uses public interface only
[ ] Test would survive internal refactor
[ ] Code is minimal for this test
[ ] No speculative features added
```
