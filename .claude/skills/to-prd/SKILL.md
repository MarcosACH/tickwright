---
name: to-prd
description: Create a PRD through user interview, codebase exploration, and module design, then submit as a GitHub issue. Use when user wants to write a PRD, create a product requirements document, or plan a new feature.
---

This skill will be invoked when the user wants to create a PRD. You may skip
steps if you don't consider them necessary.

1. Ask the user for a long, detailed description of the problem they want to
  solve and any potential ideas for solutions.

2. Explore the repo to verify their assertions and understand the current state
  of the codebase.

3. Interview the user relentlessly about every aspect of this plan until you
  reach a shared understanding. Walk down each branch of the design tree,
  resolving dependencies between decisions one-by-one.

4. Sketch out the major modules you will need to build or modify to complete the
  implementation. Actively look for opportunities to extract deep modules that
  can be tested in isolation.

A deep module (as opposed to a shallow module) is one which encapsulates a lot
of functionality in a simple, testable interface which rarely changes.

Check with the user that these modules match their expectations. Check with the
user which modules they want tests written for.

5. Once you have a complete understanding of the problem and solution, use the
  template below to write the PRD. The PRD should be submitted as a GitHub
  issue with 'ai' and 'prd' labels.

6. File the issue in `MarcosACH/tickwright` (the single repo for this project —
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

## Testing Decisions

A short bullet list. Include:

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
