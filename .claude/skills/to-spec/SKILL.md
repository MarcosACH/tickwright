---
name: to-spec
description: Synthesize the current conversation into a PRD and publish it as a GitHub issue — no interview, just synthesis of what you already know. Use when alignment is done (typically via /grill-with-docs) and you want the destination document filed.
---

This skill turns the current conversation context and codebase understanding into a **PRD** (the produced artifact is still called a PRD, not a "spec" — only the skill's invocation name is `to-spec`). It is **synthesis-only: do NOT interview the user**. Alignment is `/grill-with-docs`'s job (Phase 0); by the time you invoke this skill the shared understanding already exists, and this skill just proves you can summarise it. If you find yourself needing to ask a load-bearing question, that is a signal to stop and run `/grill-with-docs` first, not to interview here.

## Process

1. **Explore the repo** to understand the current state of the codebase, if you have not already. Use the project's domain glossary vocabulary (`CONTEXT.md`) throughout the spec, and respect the ADRs in the area you're touching. Look up facts in the code rather than asking; the decisions are already settled.

2. **Sketch the test seams.** Before writing the spec, write down the seams at which this feature will be tested — the public boundaries (`Strategy`, `MarketFeed`, `Exchange`, `EventBus`, `Store`, `Clock`, fill models; ADR-0032) where behavior is observed without reaching inside. Prefer **existing** seams to new ones, and place any new seam at the **highest** point you can. The fewer seams across the codebase, the better — the ideal is one. Feed these into the **Testing Decisions** section below.

   Check with the user that these seams match their expectations. This is the one confirmation this skill makes — it is not a re-opening of the interview.

3. **Write the PRD** using the template below, then submit it as a GitHub issue with the `ai` and `prd` labels — no additional triage label. The `prd` kind label plus the board Status is the signal Ralph reads; this repo has no single "agent-ready" label (see `docs/agents/triage-labels.md`).

4. **File the issue** in `MarcosACH/tickwright` (the single repo for this project —
  the parent PRD and all child implementation issues live here). Then, in order:

  - **Add to the project board**: `gh project item-add 2 --owner MarcosACH --url <issue-url>`.
    (The board number is canonical in `docs/agents/issue-tracker.md` — if these disagree,
    that file wins.)
  - **Set status to `Todo`**: via `gh project item-edit` (status field IDs in
    `docs/agents/issue-tracker.md`).
  - **Set assignee to `@me`**: `gh issue edit <n> -R MarcosACH/tickwright --add-assignee @me`.
    (Or pass `--assignee @me` on the original `gh issue create` call.)

  Do NOT set a GitHub Issue Type — this user-owned repo does not use them (org-only
  feature); the `prd` label is what marks the issue kind. See
  `docs/agents/issue-tracker.md` §Issue kind.

  Confirm the project URL `https://github.com/users/MarcosACH/projects/2` in the
  response.

  See `docs/agents/issue-tracker.md` for the full gh CLI conventions (creation,
  sub-issue linking via the REST API, status field IDs, assignee and issue-kind
  conventions, and the `gh project item-edit` invocation).

<prd-template>

## Problem Statement

The problem that the user is facing, from the user's perspective.

## Solution

The solution to the problem, from the user's perspective.

## User Stories

A LONG, numbered list of user stories. Each user story should be in the format
of:

1. As an <actor>, I want a <feature>, so that <benefit>

<user-story-example>
1. As a strategy author, I want the engine to deliver every market tick to my strategy in order, so that my signals are computed on a consistent view of the market
</user-story-example>

This list of user stories should be extremely extensive and cover all aspects of
the feature.

## Implementation Decisions

A short bullet list (≤ ~10 bullets) of decisions and a pointer to where each
one is recorded. PRDs are not the home for full rationale — ADRs are.

- For each load-bearing decision, write **one line** stating the choice and
  link the ADR (`docs/adr/NNNN-<slug>.md`). If no ADR exists for a decision
  the user/agent considers load-bearing, write the ADR first, then link.
- May list module names from the matched module map. Do NOT restate the
  module map's dependency graph, wire shapes, or gate ordering — link the
  map instead.
- Do NOT include file paths, function signatures, struct shapes, or code
  snippets. Those rot fast and duplicate the codebase.
- **Exception:** if a `/prototype` produced a snippet that encodes a decision
  more precisely than prose can (a state machine, reducer, schema, or type
  shape), inline the decision-rich bits within the relevant decision and note
  briefly that it came from a prototype. Not a working demo — just the part
  that pins the decision.

## Testing Decisions

A short bullet list. Include:

- **The test seams** sketched in step 2 (which public boundary each behavior is
  observed at), and whether each is existing or new.
- The test scope (which package / which test file pattern).
- Prior art (similar tests already in the codebase) by path.
- Anti-scope: behaviors deliberately left untested at this layer.

Do not enumerate every test case here — those land in the slice issue's
acceptance criteria.

## Out of Scope

One bullet per category, not one bullet per SPEC sub-section. Lean toward
"§N is out of scope except for <narrow carve-out>" over exhaustive
enumeration. The PRD is read repeatedly; long Out-of-Scope sections age
poorly as adjacent slices land.

## Further Notes

Up to ~3 bullets. Reserve for facts that do not fit elsewhere (hard wire
cuts, feature-flag posture, deliberate dependency choices). Anything that
belongs in a commit message, PR body, or ADR goes there instead.

</prd-template>
