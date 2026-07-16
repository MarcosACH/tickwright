---
name: to-issues
description: Break a plan, spec, or PRD into independently-grabbable issues on the project issue tracker using tracer-bullet vertical slices. Use when user wants to convert a plan into issues, create implementation tickets, or break down work into issues.
---

# To Issues

Break a plan into independently-grabbable issues using vertical slices (tracer bullets).

See `docs/workflow/labels.md` for the full label vocabulary (PR state, priority, domain, human gates), `docs/agents/issue-tracker.md` for the gh CLI patterns (creation, sub-issue linking, status transitions), and `docs/agents/triage-labels.md` for the canonical role → label mapping the skills speak in.

## Single repo

Tickwright is a single GitHub repo: `MarcosACH/tickwright`. The parent PRD issue and all child implementation issues live in this one repo.

- **Parent PRD issue** lives in `MarcosACH/tickwright`.
- **Child implementation issues** are filed in the same repo and linked as GitHub sub-issues of the parent.
- A single slice produces one child issue, linked as a sub-issue of the parent. Each child becomes one PR; merges happen in dependency order (blockers first).

The project board is `https://github.com/users/MarcosACH/projects/2` (**Tickwright — Workflow**). Every parent and child must be added to it with `gh project item-add 2 --owner MarcosACH --url <issue-url>`. The board number is canonical in `docs/agents/issue-tracker.md` — if these disagree, that file wins.

## Process

### 1. Gather context

Work from whatever is already in the conversation context. If the user passes an issue reference (issue number, URL, or path) as an argument, fetch it with `gh issue view <N>` and read its full body and comments.

Then look up the architecture anchor:

- List `docs/module-maps/` (skip silently if it doesn't exist). Each `*.md` file's `## Source` section names the PRD it implements — match by issue number or URL, not by filename.
- If exactly one map references the PRD you're slicing, read it in full. The module names, dependency graph, and Out-of-Scope rejections are load-bearing: slice titles and acceptance criteria must use the map's module names verbatim so downstream `tdd` agents can grep.
- If zero maps match, proceed without one but flag it to the user before drafting slices: "No module map found for this PRD — slice names will come from the PRD body and may drift from the codebase glossary. Want to run `/module-map` first?"
- If multiple match, ask the user which one is current. Do not guess.

Remember the matched map's path; you will reference it from each issue body in step 5.

### 2. Explore the codebase (optional)

If you have not already explored the codebase, do so to understand the current state of the code. Issue titles and descriptions should use the project's domain glossary vocabulary (CONTEXT.md), the module names from the matched module map, and respect ADRs in the area you're touching.

### 3. Draft vertical slices

Break the plan into **tracer bullet** issues. Each issue is a thin vertical slice that cuts through ALL integration layers end-to-end, NOT a horizontal slice of one layer.

Slices may be 'HITL' or 'AFK'. HITL slices require human interaction, such as an architectural decision or a design review. AFK slices can be implemented and merged without human interaction. Prefer AFK over HITL where possible.

<vertical-slice-rules>
- Each slice delivers a narrow but COMPLETE path through every layer (feed → strategy → exchange → engine, plus tests)
- A completed slice is demoable or verifiable on its own
- Prefer many thin slices over few thick ones
</vertical-slice-rules>

### 4. Quiz the user

Present the proposed breakdown as a numbered list. For each slice, show:

- **Title**: short descriptive name
- **Type**: HITL / AFK
- **Blocked by**: which other slices (if any) must complete first
- **User stories covered**: which user stories this addresses (if the source material has them)

Ask the user:

- Does the granularity feel right? (too coarse / too fine)
- Are the dependency relationships correct?
- Should any slices be merged or split further?
- Are the correct slices marked as HITL and AFK?

Iterate until the user approves the breakdown.

### 5. Publish the issues to the issue tracker

For each approved slice, publish a new issue to the issue tracker. Use the issue body template below.

Before applying any labels, provision them once with `.agents/tools/ensure-labels.sh` (idempotent) — `gh issue create` fails on labels that don't exist yet.

Apply labels exactly (see `docs/workflow/labels.md` for the canonical list):

- All issues: `needs-triage`, `ai`
- AFK implementation issue: exactly one priority label
  - `tracer` for normal vertical slices
  - `infra` for shared scaffolding/schema/foundation work
  - `polish` for low-risk cleanup/refinement
  - `critical` only for bugs/blockers that stop active work
- HITL issue: `ready-for-human` or `needs-human-validation`; do not add `tracer`
- Add domain labels when relevant. Heavy labels (route to the strongest model, canonical in `docs/workflow/labels.md`): `recovery`, `engine`, `exchange`, `chain`, `concurrency`, `unsafe`.

These labels drive Ralph selection and model choice. Do not invent alternate priority labels.

Publish issues in dependency order (blockers first) so you can reference real issue identifiers in the "Blocked by" field.

```bash
gh issue create -R MarcosACH/tickwright --title "<title>" --body-file <file> --label "<labels>"
```

After each `gh issue create`, immediately, in order:

1. Add the new issue to the project board:
   `gh project item-add 2 --owner MarcosACH --url <issue-url>`
   `gh issue create` does NOT do this; an unlinked issue is invisible to triage and Ralph.
2. Set status to `Todo` via `gh project item-edit` (status field IDs in `docs/agents/issue-tracker.md`).
3. Set assignee to `@me`:
   ```bash
   gh issue edit <n> -R MarcosACH/tickwright --add-assignee @me
   ```
   (Or pass `--assignee @me` on the original `gh issue create` call.) Unassigned issues are treated as triage-pending and Ralph will not pick them up.
4. If the slice has a parent issue (the source PRD), link the new issue as a real GitHub sub-issue of the parent via REST:
   ```bash
   CHILD_DB_ID=$(gh api repos/MarcosACH/tickwright/issues/<child> --jq '.id')
   gh api -X POST repos/MarcosACH/tickwright/issues/<parent>/sub_issues \
     -F sub_issue_id=$CHILD_DB_ID
   ```
   Note `-F` (numeric) not `-f` (string) — the API rejects a string-typed `sub_issue_id`. `sub_issue_id` is the **database id** (`.id`, an integer), not the GraphQL node id (`I_kwDO…`) and not the issue number — always resolve it via `--jq '.id'`. The `## Parent` body section is human-readable redundancy and does NOT create the GitHub sub-issue link on its own.
5. If the slice is blocked by another (its `## Blocked by` names one), create GitHub's real dependency relationship alongside the `blocked` label. Slices are published in dependency order (blockers first, per the preamble above), so the blocker already has a real issue number here:
   ```bash
   BLOCKER_DB_ID=$(gh api repos/MarcosACH/tickwright/issues/<blocker> --jq '.id')
   gh api -X POST repos/MarcosACH/tickwright/issues/<blocked>/dependencies/blocked_by \
     -F issue_id=$BLOCKER_DB_ID
   ```
   `issue_id` is the **database id** (`.id`), not the issue number — same id-type trap as `sub_issue_id`; passing the number silently links an unrelated repo's issue. Keep both the `blocked` label (what the picker filters on) and this relationship (what feeds the UI, merge gating, and the unblock sweep). The `## Blocked by` body section does NOT create the link on its own. See *Link a blocker* in `docs/agents/issue-tracker.md`.

After publishing all slices, verify with `gh issue view <n> --json projectItems` (project linkage), `gh api repos/MarcosACH/tickwright/issues/<parent>/sub_issues --jq '[.[] | {number, title}]'` (sub-issue linkage), and `gh api repos/MarcosACH/tickwright/issues/<blocked>/dependencies/blocked_by --jq '.[] | "#\(.number)"'` (blocker linkage) for at least one issue of each kind.

### Body authoring rules (agent-optimized, not human-optimized)

Issues are read by agents that already have the module map, ADRs, SPEC, and `CONTEXT.md` available. Prose that restates those sources rots and misleads. Keep issue bodies lean.

**`What to build`** — one paragraph of intent (the goal, the user-visible delta), then 5–10 bullets at the *behavior* level. Name the new modules and the wire-shape delta if any; otherwise link.

Do NOT put any of the following in the issue body — they live in the module map / ADR / SPEC:
- struct shapes, field numbers, event/payload encoding rules
- file paths, function signatures, gate-ordering steps
- failure-status mapping tables, dependency graphs
- multi-paragraph rationale (that's the ADR's job)

If a section would re-state more than ~3 lines of the module map, replace with `see <map-path> §<heading>`.

**`Acceptance criteria`** — observable outcomes only. CLI exits, event/status codes, exchange/paper-exchange effects, named test functions, file-permission bits. NOT implementation prescriptions ("module X depends only on Y", "handler runs gates in order Z") — those are code-review checks, not acceptance.

**Single source of truth, in priority order:**
- module map → module names, dependency graph, wire shapes, gate ordering
- ADRs → decisions and rationale
- `SPEC.md` / `CONTEXT.md` → requirements and glossary
- issue body → goal, scope cut, observable acceptance, links

If a fact lives in two places, the issue is the wrong place. Move or delete.

<issue-template>
## Parent

A reference to the parent issue on the issue tracker (if the source was an existing issue, otherwise omit this section).

## Architecture anchor

Path to the module map this slice belongs to (e.g. `docs/module-maps/<feature-slug>.md`). Implementing agents must read it before starting; module names in this issue come from it. Omit this section only if step 1 found no map.

## What to build

One paragraph of intent + 5–10 behavior-level bullets. No struct/field/path/signature restatement — link the module map.

## Acceptance criteria

- [ ] Observable outcome 1 (CLI exit, event status, paper-exchange delta, named test)
- [ ] Observable outcome 2
- [ ] Observable outcome 3

## Blocked by

- A reference to the blocking ticket (if any). Human-readable redundancy — the real dependency is created via the API in the post-create step list, not by this section.

Or "None - can start immediately" if no blockers.

</issue-template>

Do NOT close or modify any parent issue.
