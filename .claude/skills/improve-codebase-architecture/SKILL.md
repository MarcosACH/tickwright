---
name: improve-codebase-architecture
description: Find deepening opportunities in a codebase, informed by the domain language in CONTEXT.md and the decisions in docs/adr/. Use when the user wants to improve architecture, find refactoring opportunities, consolidate tightly-coupled modules, or make a codebase more testable and AI-navigable.
---

# Improve Codebase Architecture

Surface architectural friction and propose **deepening opportunities** — refactors that turn shallow modules into deep ones. The aim is testability and AI-navigability.

## Glossary

Use these terms exactly in every suggestion. Consistent language is the point — don't drift into "component," "service," "API," or "boundary." Full definitions in [LANGUAGE.md](LANGUAGE.md).

- **Module** — anything with an interface and an implementation (function, class, module, package, slice).
- **Interface** — everything a caller must know to use the module: types, invariants, error modes, ordering, config. Not just the type signature.
- **Implementation** — the code inside.
- **Depth** — leverage at the interface: a lot of behaviour behind a small interface. **Deep** = high leverage. **Shallow** = interface nearly as complex as the implementation.
- **Seam** — where an interface lives; a place behaviour can be altered without editing in place. (Use this, not "boundary.")
- **Adapter** — a concrete thing satisfying an interface at a seam.
- **Leverage** — what callers get from depth.
- **Locality** — what maintainers get from depth: change, bugs, knowledge concentrated in one place.

Key principles (see [LANGUAGE.md](LANGUAGE.md) for the full list):

- **Deletion test**: imagine deleting the module. If complexity vanishes, it was a pass-through. If complexity reappears across N callers, it was earning its keep.
- **The interface is the test surface.**
- **One adapter = hypothetical seam. Two adapters = real seam.**

This skill is _informed_ by the project's domain model. The domain language gives names to good seams; ADRs record decisions the skill should not re-litigate.

## Process

### 1. Explore

Read the project's domain glossary and any ADRs in the area you're touching first.

Then use the Agent tool with `subagent_type=Explore` to walk the codebase. Don't follow rigid heuristics — explore organically and note where you experience friction:

- Where does understanding one concept require bouncing between many small modules?
- Where are modules **shallow** — interface nearly as complex as the implementation?
- Where have pure functions been extracted just for testability, but the real bugs hide in how they're called (no **locality**)?
- Where do tightly-coupled modules leak across their seams?
- Which parts of the codebase are untested, or hard to test through their current interface?

Apply the **deletion test** to anything you suspect is shallow: would deleting it concentrate complexity, or just move it? A "yes, concentrates" is the signal you want.

### 2. Present candidates

Present a numbered list of deepening opportunities. For each candidate:

- **Files** — which files/modules are involved
- **Problem** — why the current architecture is causing friction
- **Solution** — plain English description of what would change
- **Benefits** — explained in terms of locality and leverage, and also in how tests would improve

**Use CONTEXT.md vocabulary for the domain, and [LANGUAGE.md](LANGUAGE.md) vocabulary for the architecture.** If `CONTEXT.md` defines "Order," talk about "the Order intake module" — not "the FooBarHandler," and not "the Order service."

**ADR conflicts**: if a candidate contradicts an existing ADR, only surface it when the friction is real enough to warrant revisiting the ADR. Mark it clearly (e.g. _"contradicts ADR-0007 — but worth reopening because…"_). Don't list every theoretical refactor an ADR forbids.

Do NOT propose interfaces yet. Ask the user: "Which of these would you like to explore?"

### 3. Grilling loop

Once the user picks a candidate, drop into a grilling conversation. Walk the design tree with them — constraints, dependencies, the shape of the deepened module, what sits behind the seam, what tests survive.

Side effects happen inline as decisions crystallize:

- **Naming a deepened module after a concept not in `CONTEXT.md`?** Add the term to `CONTEXT.md` — same discipline as `/grill-with-docs` (see [CONTEXT-FORMAT.md](../grill-with-docs/CONTEXT-FORMAT.md)). Create the file lazily if it doesn't exist.
- **Sharpening a fuzzy term during the conversation?** Update `CONTEXT.md` right there.
- **User rejects the candidate with a load-bearing reason?** Offer an ADR, framed as: _"Want me to record this as an ADR so future architecture reviews don't re-suggest it?"_ Only offer when the reason would actually be needed by a future explorer to avoid re-suggesting the same thing — skip ephemeral reasons ("not worth it right now") and self-evident ones. See [ADR-FORMAT.md](../grill-with-docs/ADR-FORMAT.md).
- **Want to explore alternative interfaces for the deepened module?** See [INTERFACE-DESIGN.md](INTERFACE-DESIGN.md).

### 4. Open the PR, link it to the issue, and move issue to In Review

This skill owns the PR opening step, the issue-link verification, and the `In Review` project-board transition in the day-shift pipeline. After the deepening review is settled and the branch is ready (typically pushed by a prior `/tdd` cycle):

1. **Confirm the PR base branch with the user before running `gh pr create`.** Never silently default to `main` or `master`, and never assume the branch's tracked upstream is the right merge target. Ask explicitly which branch the PR should merge into — examples: `main`, an in-flight epic branch like `feature/<epic-slug>`, or a parent slice's branch when the parent hasn't merged yet. Show the candidates you can see (the branch's actual fork point via `git merge-base`, the parent PRD's target branch if known, sibling slices' merge targets from recent `gh pr list --state merged` entries) and ask. Only after the user confirms, pass `--base <branch>` explicitly — do not omit the flag.

2. **Open the PR** with `Closes #<n>` on the **first non-empty line** of the body, `--assignee @me`, and the initial state label `ralph:needs-review`. Example:
   ```sh
   gh pr create -R MarcosACH/tickwright --base <base-branch> --assignee @me \
     --label ralph:needs-review --title "..." --body "..."
   ```
   The `pr-policy` CI check fails any PR that does not carry **exactly one** `ralph:*` state label and a `Closes #N` body — set both at creation, not after. If the label doesn't exist yet, provision it with `.agents/tools/ensure-labels.sh` (idempotent).
   See [docs/agents/issue-tracker.md](../../../docs/agents/issue-tracker.md) §"Creating PRs" for the full convention. Unassigned PRs are treated as drive-by; Ralph and the review pipeline expect a named owner.

3. **Verify the link populated.** The `Closes` keyword in the body is necessary but not sufficient — GitHub's parser occasionally misses it, especially on PRs that don't target the default branch. After opening, poll `closingIssuesReferences` for up to ~30s:
   ```sh
   gh api graphql -f query='query { repository(owner:"MarcosACH", name:"tickwright") { pullRequest(number:<PR>) { closingIssuesReferences(first:5) { nodes { number } } } } }' \
     --jq '.data.repository.pullRequest.closingIssuesReferences.nodes'
   ```
   - **Non-empty** → linked. Done with this step.
   - **Empty after polling** → GitHub did not parse the keyword. Apply the fallback below. Do not proceed to step 5 with an unlinked PR.

4. **Fallback if the link is empty.** Apply in order until the link populates:
   1. Re-edit the body with `gh pr edit -R MarcosACH/tickwright <PR> --body "<body>"` so the `Closes` keyword is on the first non-empty line, with no leading whitespace, no zero-width characters, and a blank line after.
   2. Close-then-reopen the PR (`gh pr close` + `gh pr reopen`) to nudge the parser to re-run.
   3. If still empty, drop a comment on the parent issue that names the PR explicitly so the cross-reference is unambiguous in the UI even when the development-panel link is missing:
      ```sh
      gh issue comment <n> -R MarcosACH/tickwright --body "Linked PR: https://github.com/MarcosACH/tickwright/pull/<PR> (Closes #<n>)"
      ```
   4. Last resort: recreate the PR (close #N, open #N+1). Treat this as a real fix to the parser miss, not a routine action — the first three options are cheaper.

5. **Move issue Status to `In Review`** per [docs/agents/issue-tracker.md](../../../docs/agents/issue-tracker.md).

`/tdd` is responsible for the `In Progress` transition at pickup; this skill is responsible for `In Review` at PR open. Linking is **mandatory** before the transition — an unlinked PR is invisible to Ralph and the reviewer pipeline. Keep all five steps here so the PR reflects the architectural review, not just the green-test snapshot.
