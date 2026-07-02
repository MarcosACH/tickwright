---
name: code-review
description: Python-focused code review with structured PR comments and a merge gate. Use when reviewing a branch, PR, or diff before merge. Produces ID'd comments (BLOCKING/WARN/NIT) that the implementing agent must resolve before merge.
---

# Python Code Review

## Philosophy

**Push standards explicitly.** The reviewer carries the rules into the prompt; do not rely on the implementing agent having read them. Standards explicit in the prompt is the right mode for review (the implementation phase is the time to pull from references).

**Comments are contracts.** Every finding gets an ID, a severity, a category, and a fix. The implementing agent must reply with `resolved` + commit SHA. A merge gate enforces this — see [Merge Gate](#merge-gate).

**Reviewers don't fix code.** Write the comment; let the implementing agent fix it. If the reviewer fixes inline, the loop breaks and quality degrades.

## When to Use

- Before merging any branch into `main`
- After an AFK ralph-loop run to QA the diff
- On a GitHub PR (`/code-review <PR#>`)
- Re-running after the implementing agent claims `resolved` (merge gate verification)

Skip for: doc-only changes, generated files, dependency bumps with no code edits.

## Review Workflow

### 1. Triage

Before reading code:

- [ ] Read the issue linked in the PR description (`Closes #N`).
- [ ] Fetch its **parent issue** — slice issues are usually sub-issues of a PRD, and the PRD has cross-slice context (what's deferred to later slices, what's architecturally locked) that recalibrates severity. Without it, you risk flagging intentionally-deferred work as "missing."
  ```bash
  gh api graphql -f query='{ repository(owner:"MarcosACH",name:"tickwright") { issue(number:N) { parent { number title body } } } }'
  ```
  If `parent` is null, the linked issue is the spec. Otherwise read the parent before the diff and treat its "Out of scope" sections as hard constraints on what *not* to flag.
- [ ] List the ADRs (`docs/adr/`) covering the touched modules and read them via `.agents/tools/doc-slice` — they are the densest source of review criteria in this repo. A diff that violates an ADR's decision is BLOCKING (cite the ADR in the finding).
- [ ] If no context at all, **STOP and ask the user.**
- [ ] Establish the optimization axis: correctness, performance, maintainability? Different axes change which findings are BLOCKING.
- [ ] Identify the diff scope: `git diff main...HEAD` (or `gh pr diff <PR#>`).
- [ ] List touched modules. Cap review at one module at a time to stay in the smart zone.

#### Workspace discipline (PR target)

Review is **read-only against the user's working tree**. Do not mutate refs.

- Do **not** run `git fetch origin pull/<PR#>/head:<branch>`, `git checkout <pr-branch>`, `gh pr checkout`, or any command that creates or switches branches. The user manages their own branch state; a reviewer leaving behind `pr-<N>-review` branches is noise.
- To inspect PR contents at the PR head, fetch the ref without naming it and read through `FETCH_HEAD`:
  ```bash
  gh pr view <PR#> --json headRefOid --jq .headRefOid   # → <SHA>
  git fetch origin pull/<PR#>/head                       # populates FETCH_HEAD only
  git show <SHA>:path/to/file.py                         # read a file at PR head
  gh pr diff <PR#>                                       # full diff
  ```
  `git fetch` without a destination ref does not create a local branch.
- If the PR's `headRefName` already exists locally and points at the same SHA as the PR head, just read files via `git show <SHA>:<path>` — no fetch needed.
- After the review, verify no stray refs remain: `git branch --list 'pr-*-review'` should be empty.

### 2. Mechanical pass (push to tools, not humans)

Run these and treat any failure as BLOCKING. The list mirrors the authoritative CI gate (`.github/workflows/ci.yml`) — if the two ever disagree, ci.yml wins and this list needs updating:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy .
uv run lint-imports   # ADR-0032 dependency-direction boundaries — a build-failing gate
uv run pytest --cov --cov-report=term-missing --cov-fail-under=90
```

ci.yml also runs `python -m compileall -q src` (byte-compile smoke) and a
`pip-audit` supply-chain scan of the runtime deps; those are CI-only guards, not
part of the reviewer's local mechanical pass.

A review must not declare `Merge: READY` on a diff that would fail CI — the coverage gate (≥90% on the core) is part of the mechanical pass, not a style preference. Reviews must not flag findings ruff/mypy already catch — that is wasted attention.

### 3. Substantive pass

Walk the diff against the [Python Checklist](#python-checklist) below. For each finding, write a structured comment (see [Comment Format](#comment-format)).

### 4. Emit review

For a GitHub PR target, post the review directly via `gh pr review` — do **not** write a local file. For a local branch with no PR, write `review-<short-sha>.md` at the repo root. See [Output](#output).

### 5. Merge gate

Before merge, re-run the skill against the new HEAD. Every prior `BLOCKING` must be `resolved` AND verified. See [Merge Gate](#merge-gate).

---

## Comment Format

Every finding is a structured block. The implementing agent parses these IDs to drive fixes; deviating from the format breaks the loop.

```
### [SEVERITY] R### — path/to/file.py:LINE — Short title
**Category**: errors | typing | async | concurrency | performance | api | testing | tooling | docs | security
**Why**: One sentence on the concrete failure mode (e.g., "bare except masks KeyboardInterrupt").
**Fix**: One sentence with the prescribed change. Code snippet if non-obvious.
**Status**: open
```

### Severity rules

| Severity | Definition | Merge effect |
|----------|------------|--------------|
| BLOCKING | Correctness, data loss, exception swallowing in critical paths, missing tests for new behavior, ruff/mypy/pytest failures, breaking changes to a public API without justification, security defects | **Blocks merge.** Must be `resolved` + verified. |
| WARN | Idiom violation, performance smell, missing edge-case test, weak API ergonomics, missing type hints on public surfaces | Should fix. May be waived only with explicit `**Status**: waived — <reason>` from the user. |
| NIT | Style preference, naming, doc nits not covered by ruff | Optional. Default to skipping unless they cluster around one symbol. |

### ID rules

- Sequential per review file: `R001`, `R002`, …
- Stable across re-reviews: if `R007` is fixed, do **not** renumber.
- New findings on re-review continue the sequence.

---

## Output

The review body has this exact shape regardless of where it lands:

```markdown
# Review of <branch> @ <short-sha>
Reviewer: code-review skill
Reviewed: <ISO date>
Optimization axis: <correctness | perf | maintainability>

## Mechanical
- ruff format: pass | FAIL
- ruff check: pass | FAIL
- mypy: pass | FAIL
- pytest: pass | FAIL (N failures)

## Findings

<comment blocks here, sorted BLOCKING → WARN → NIT>

## Summary
- BLOCKING: N open
- WARN: N open
- NIT: N open
- Merge: BLOCKED | READY

Ralph-State: changes-requested | ready
```

`Ralph-State` is machine-readable:

- `changes-requested` when any BLOCKING or WARN is open or any finding regressed
- `ready` only when BLOCKING/WARN are resolved or explicitly waived, with no regressions

**GitHub PR review (preferred)** → pipe the body straight into `gh pr review` and stop. No local file.
- Any BLOCKING: `gh pr review <PR#> --request-changes --body "$(cmd-that-prints-review)"`
- Otherwise: `gh pr review <PR#> --comment --body "..."`
- If `--request-changes` is rejected because the PR is the user's own, retry with `--comment`.
- For line-anchored comments, post each finding via `gh api` against the PR's review-comments endpoint.
- Set the PR's `ralph:*` state label from the footer. Every open PR carries **exactly one** `ralph:*` label (enforced by the `pr-policy` CI check); set the new one and clear the rest in a single edit:
  ```bash
  # Ralph-State: changes-requested
  gh pr edit <PR#> -R MarcosACH/tickwright --add-label ralph:changes-requested \
    --remove-label ralph:needs-review,ralph:review-addressed,ralph:ready
  # Ralph-State: ready
  gh pr edit <PR#> -R MarcosACH/tickwright --add-label ralph:ready \
    --remove-label ralph:needs-review,ralph:changes-requested,ralph:review-addressed
  ```
  See [docs/agents/issue-tracker.md](../../../docs/agents/issue-tracker.md) §"Ralph PR lifecycle" for the state machine.

**Local branch with no PR** → write `review-<short-sha>.md` at the repo root. The implementing agent reads and edits this file to drive fixes.

Do not produce both. A local file alongside a PR comment forks the source of truth; pick one based on the target.

---

## Merge Gate

The implementing agent **cannot merge** while any `BLOCKING` finding has `**Status**: open`. Enforcement:

1. Implementing agent fixes a finding, commits, and records the resolution:
   ```
   **Status**: resolved — <commit-sha>
   ```
   - PR target: post a reply on the PR review thread (or a new PR comment) with the resolved-line per finding ID. Do not edit prior review comments — append.
   - Local-file target: edit `review-<sha>.md` in place.
2. Re-run this skill against the new HEAD. The skill:
   - For PR targets, fetches the prior review body via `gh pr view <PR#> --json reviews,comments` and reconstructs status from the conversation (latest status per `R###` wins).
   - Verifies each `resolved` finding is actually fixed at the cited SHA. If not, mark `**Status**: regressed` and bump severity.
   - Adds new findings discovered in the new diff (continue ID sequence).
   - Posts the updated review as a new PR comment (or rewrites the local file).
3. Merge is permitted only when the latest review shows `BLOCKING: 0 open` AND no `regressed` entries.
4. The latest review must also show `Ralph-State: ready` and the PR must be labeled `ralph:ready` (every PR uses the `ralph:*` state machine — see `docs/workflow/labels.md`).

**WARN waivers** require the user (not the implementing agent) to write:
```
**Status**: waived — <one-line justification>
```
A waiver from the agent itself does not count.

---

## Python Checklist

Use this as the substantive-pass spine. Each item maps to a `Category` value.

### Error handling
- [ ] **No bare `except:` or `except Exception:`** without re-raise or explicit narrow rationale. BLOCKING in request/IO paths.
- [ ] No `assert` for runtime validation (asserts are stripped under `-O`). Use explicit `raise` with a typed exception.
- [ ] Exception chains preserved: `raise NewError(...) from e`, not `raise NewError(...)`.
- [ ] No `pass`-only except blocks. If you really mean to swallow, log it and explain why.
- [ ] Errors at boundaries include the offending value when safe to log; never log API keys, signing keys, or exchange secrets.

### Typing
- [ ] All new public functions/methods have full type hints (parameters + return).
- [ ] `Any` is justified or replaced with a `Protocol` / `TypedDict` / generic.
- [ ] No `# type: ignore` without a comment explaining why.
- [ ] Generic types parameterized: `dict[str, int]`, not bare `dict`.
- [ ] Optional vs `T | None` consistent with the rest of the module.

### Async (asyncio)
- [ ] No blocking I/O (`open()`, `requests`, `time.sleep`) inside `async def`. Use `aiofiles`, `aiohttp`, `asyncio.sleep`.
- [ ] No `asyncio.run` inside an already-running event loop. Use `await` or `asyncio.create_task`.
- [ ] Cancellation: tasks created with `asyncio.create_task` have a documented cancel/await path. Don't fire-and-forget.
- [ ] No sync `threading.Lock` held across `await`. Use `asyncio.Lock` if the lock spans an await; otherwise narrow the critical section.
- [ ] Long-running loops include `await asyncio.sleep(0)` yield points.

### Concurrency / shared state
- [ ] No mutable global state mutated from multiple workers without explicit lock/queue/event-bus discipline.
- [ ] Async resources (sessions, connections) not shared across asyncio tasks without their own lifecycle.
- [ ] Idempotent retries: re-running a handler must not double-submit / double-place orders.

### Performance
- [ ] No `+=` string concatenation in hot loops. Use `"".join(...)` or `io.StringIO`.
- [ ] No `list(generator)` when an iterator suffices.
- [ ] `dict`/`set` lookups over `list` `in` checks for large collections.
- [ ] No repeated network / RPC calls inside a loop. Batch or cache.
- [ ] Decimal vs float: financial calculations use `Decimal`. Float comparisons forbidden in money paths.

### API design
- [ ] Public modules expose a narrow `__all__`. Cross-package imports go through the public surface, not through a leaf module's private helper.
- [ ] No `from x import *` in production code.
- [ ] Constructors with >3 params or any optional params use keyword-only args (`def __init__(self, *, a, b, c)`).
- [ ] Don't expose third-party types in your public API where you can wrap (e.g. don't return a raw exchange-SDK response object from a service layer).
- [ ] Public dataclasses prefer `frozen=True` unless mutation is intentional.

### Testing
- [ ] New behavior has a test exercising the **public interface** (per `tdd` skill).
- [ ] No mocks for internal collaborators. Mock only at process boundaries (network, FS, clock, the exchange SDK, the event-bus transport at the client level).
- [ ] `pytest.raises` asserts on the exception type AND a substring of the message where the message is part of the contract.
- [ ] Parametrized tests over copy-pasted near-duplicates.
- [ ] No `time.sleep` in tests, and no freezing/patching time. Tests drive time through the injected `Clock` (`ManualClock`) — ADR-0005 makes direct `time.time()` / `asyncio.sleep` a banned pattern in engine code.

### Security
- [ ] No secrets in logs or in commit (`SECRET_KEY`, `ENCRYPTION_KEY`, exchange API keys, signing keys).
- [ ] `subprocess` calls do not pass user input through `shell=True`.
- [ ] Any SQL goes through parameterized queries, never f-string interpolation.
- [ ] HTTPS only for the exchange / external APIs.

### Tooling & docs
- [ ] `uv run ruff check .` clean (mechanical pass).
- [ ] `uv run ruff format --check .` clean.
- [ ] `uv run mypy .` clean (or new `# type: ignore` lines explained).
- [ ] No `# noqa` without a rule code and reason.
- [ ] First sentence of each public function/class docstring is one line, ~15 words.
- [ ] **Docs-sync**: if the diff changes behavior or conventions documented elsewhere (`CLAUDE.md`, `docs/agents/`, `docs/workflow/`, `docs/adr/`, `CONTEXT.md`, `.claude/skills/`, CI workflows), the same PR updates those files. A stale doc left behind is WARN; a contradicted canonical source (e.g. an ADR or `issue-tracker.md`) is BLOCKING.

### Tickwright invariants (BLOCKING if regressed)

Walk the canonical list in [docs/agents/invariants.md](../../../docs/agents/invariants.md) — saga idempotency, crash-safe recovery, reconciliation freeze, explicit rejections, per-symbol ordering, deterministic paper exchange. Each entry cites its ADR; cite the ADR in the finding. Do not restate the list here — the shared file is the single source.

---

## Pitfalls (annotated)

For the worst offenders — bare except, mutable default args, sync I/O in async, mocking internals, `from x import *`, mutable shared state — see [pitfalls.md](pitfalls.md). Each shows the bad pattern, the fix, and which checklist item it maps to.

---

## Anti-patterns in the review itself

Watch your own output:

- **Don't fix the code.** Write a comment. The implementing agent fixes.
- **Don't flag what ruff/mypy catches.** Mechanical pass owns those.
- **Don't WARN a hypothetical.** Cite the actual line. If you can't, drop the finding.
- **Don't NIT-pile.** If the diff has >5 NITs, escalate one as WARN ("inconsistent naming across module") and drop the rest.
- **Don't waive your own findings.** Only the user waives.
