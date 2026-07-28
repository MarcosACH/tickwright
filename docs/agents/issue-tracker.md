# Issue tracker: GitHub

Issues and PRDs for Tickwright live as GitHub issues in a **single repository** — `MarcosACH/tickwright`. Use the `gh` CLI for all operations. All issues are tracked on a single user project board: **Tickwright — Workflow** (`https://github.com/users/MarcosACH/projects/2`).

## Issue routing

- **Parent PRDs** and **child slice issues** both live in `MarcosACH/tickwright`.
- A PRD breaks into one or more child vertical-slice issues, all linked as GitHub sub-issues of the parent.
- Every slice is a vertical tracer through the relevant layers (event bus → strategy → exchange adapter → reconciliation), filed as its own child issue under the PRD.

## Anatomy of a well-formed issue

Comprehensiveness is not optional. An issue missing metadata is invisible to triage and the picker; an issue with a thin body cannot be picked up without a round-trip. Whoever files an issue — a skill or a human — is responsible for filling **all** of the following before the issue counts as created. The `gh` commands for each field are in Conventions below.

**Required metadata on every new issue:**

| Field | PRD (`to-spec`) | Slice (`to-tickets`) | Bug |
| --- | --- | --- | --- |
| **Title** | imperative capability name (e.g. "Crash-safe order-lifecycle saga") | the single vertical behavior (e.g. "Reject limit order when price crosses band") | observed defect (e.g. "Paper exchange double-fills on restart") |
| **Kind label** | `prd` | one priority label — `tracer` (default) / `critical` / `infra` / `polish` | `bug` |
| **Domain label(s)** | every heavy label that applies (`engine`, `exchange`, `recovery`, `chain`, `concurrency`, `unsafe`) | same | same |
| **Assignee** | `@me` | `@me` | `@me` |
| **Project + Status** | added to project 2, Status `Todo` (or `Backlog` if deferred) | same | same |
| **Parent link** | — | real GitHub sub-issue of its PRD | — |
| **Body** | PRD structure below | slice structure below | `bug_report.md` template |

Milestones are **not** used; sequencing comes from Status plus the `blocked` label (see picker order in [`triage-labels.md`](./triage-labels.md)).

**PRD body** (parent `prd` issue):

- **Problem / motivation** — what is missing or broken, and why it matters now.
- **Goal & scope** — the target outcome, with an explicit **Out of scope** list.
- **Vertical slices** — the child issues this decomposes into (linked as sub-issues by `to-tickets`).
- **Acceptance criteria** — observable, testable conditions for "done".
- **Affected layers** — the feed → strategy → exchange → engine touch-points.
- **References** — ADRs (`docs/adr/…`), CONTEXT.md terms, the module map.
- **Non-goals / risks** — anything deliberately excluded, and any `unsafe` invariant it touches.

**Slice body** (child implementation issue):

- **Context** — one line of why, plus a `## Parent` link to the PRD.
- **Behavior** — the one vertical behavior as Given/When/Then acceptance criteria.
- **Layers touched** — which of feed → strategy → exchange → engine this slice crosses (it must cross every relevant one — vertical-slice policy).
- **Test plan** — the failing test to write first (TDD red), plus property/edge cases.
- **`## Blocked by`** — issues that must merge first. Human-readable redundancy for the real GitHub dependency relationship (see *Link a blocker* in Conventions), not the mechanism: the `blocked` label is what drives the picker, and the dependency is what feeds everything outside it.
- **References** — the parent PRD, relevant ADRs, CONTEXT.md terms.

## Conventions

- **Create an issue**: `gh issue create -R MarcosACH/tickwright --title "..." --body "..."`. Use a heredoc for multi-line bodies. Populate the title, body, labels, and assignee to match **Anatomy of a well-formed issue** above — a bare title + body is not a complete issue. **`gh issue create` does NOT add the issue to the project board** — you must add it explicitly with `gh project item-add 2 --owner MarcosACH --url <issue-url>` immediately after creation. New project items have no Status until an agent sets one; the `to-spec` and `to-tickets` skills set newly-filed issues to `Todo`. Use `Backlog` only for items you want to defer (noted but not yet ready to be picked). An issue that is not on the project board is invisible to triage and Ralph; treat creation-without-linking as a bug.
- **Assignee**: every newly-filed issue (parent PRDs and child slices) must have an assignee set immediately after creation. The convention is `@me` — the GitHub user whose token the `gh` CLI is authenticated as (the project owner running the workflow). `gh issue create --assignee @me` works for the create step; for issues already created, use `gh issue edit <n> -R MarcosACH/tickwright --add-assignee @me`. Unassigned issues are treated as triage-pending and Ralph will not pick them up.
- **Issue kind (by label, not GitHub Issue Type)**: this repo is user-owned, and GitHub custom Issue Types are an organization-only feature, so they are **not** used here. Distinguish kind by label instead — every newly-filed issue must carry exactly one kind:
  - **PRDs** (filed by `to-spec`) → `prd`
  - **Child slice issues** (filed by `to-tickets`) → one priority label (`tracer` by default; `critical` / `infra` / `polish` as appropriate) and **never** `prd`
  - **Bug reports** → `bug`

  Apply at creation with `gh issue create --label prd ...`, or afterward with `gh issue edit <n> -R MarcosACH/tickwright --add-label prd`. The parent-vs-child distinction is thus `prd` (parent) versus a priority label (child slice).
- **Link as a sub-issue of a parent**: when an issue has a parent (e.g. a slice ticket under its PRD), make it a real GitHub sub-issue, not just a `## Parent` text reference. The CLI has no `gh sub-issue` shorthand; use the REST API:
  ```sh
  CHILD_DB_ID=$(gh api repos/MarcosACH/tickwright/issues/<child-number> --jq '.id')
  gh api -X POST repos/MarcosACH/tickwright/issues/<parent-number>/sub_issues \
    -F sub_issue_id=$CHILD_DB_ID
  ```
  Note `-F` (numeric) not `-f` (string) — the API rejects a string-typed `sub_issue_id`. Verify with:
  ```sh
  gh api repos/MarcosACH/tickwright/issues/<parent>/sub_issues \
    --jq '[.[] | {number, title, repo: .repository.full_name}]'
  ```
  The `## Parent` body section in the issue template is human-readable redundancy, not a substitute for the API link.
- **Link a blocker**: when an issue is blocked by another (e.g. slice B must merge after slice A), create GitHub's real issue-dependency relationship, not just a `## Blocked by` text reference. Keep both: the `blocked` label is what the picker filters on (GitHub's dependency does **not** drive the picker), and the relationship is what feeds the UI's blocked-by panel, GitHub's merge gating, and the unblock sweep in [`triage-labels.md`](./triage-labels.md). The CLI has no shorthand; use the REST API:
  ```sh
  BLOCKER_DB_ID=$(gh api repos/MarcosACH/tickwright/issues/<blocker-number> --jq '.id')
  gh api -X POST repos/MarcosACH/tickwright/issues/<blocked-number>/dependencies/blocked_by \
    -F issue_id=$BLOCKER_DB_ID
  ```
  `issue_id` is the numeric **database id** (`.id`), not the issue number — the same trap as `sub_issue_id` above. Passing the number would not error; it would silently link whatever issue holds that database id, in some unrelated repo. Always resolve it via `--jq '.id'`. Verify from either direction:
  ```sh
  gh api repos/MarcosACH/tickwright/issues/<blocked>/dependencies/blocked_by --jq '.[] | "#\(.number)"'
  gh api repos/MarcosACH/tickwright/issues/<blocker>/dependencies/blocking  --jq '.[] | "#\(.number)"'
  ```
  The `## Blocked by` body section in the issue template is human-readable redundancy, not a substitute for the API link.
- **Read an issue**: `gh issue view <n> -R MarcosACH/tickwright --comments`.
- **List issues**: `gh issue list -R MarcosACH/tickwright --state open --json number,title,body,labels --jq '[.[] | {number, title, labels: [.labels[].name]}]'`.
- **Comment**: `gh issue comment <n> -R MarcosACH/tickwright --body "..."`.
- **Apply / remove labels**: `gh issue edit <n> -R MarcosACH/tickwright --add-label "..."` / `--remove-label "..."`.
- **Close**: never manually — the PR's `Closes #N` does it on merge. The exceptions are the issues that have **no merge event of their own**, so no `Closes #N` can ever reach them: a **parent PRD**, closed deliberately at release (see [Linking PRs to issues](#linking-prs-to-issues)), and a **wayfinder ticket**, closed on resolution, with its **map** closed on hand-off (see [Wayfinding operations](#wayfinding-operations)). Never a child slice issue.

## When a skill says "publish to the issue tracker"

Create a GitHub issue in `MarcosACH/tickwright`, then add it to the project board. See [`to-tickets`](../../.claude/skills/to-tickets/SKILL.md) for the full creation flow.

## When a skill says "fetch the relevant ticket"

Run `gh issue view <number> -R MarcosACH/tickwright --comments`.

## Creating PRs

- **Assignee**: **every** PR — opened by any workflow skill (e.g. `/improve-codebase-architecture` — see [skill section 4](../../.claude/skills/improve-codebase-architecture/SKILL.md)), by `/tdd`, or by hand for a one-off chore/fix — must set `--assignee @me`. There is no "manual PR" exemption. Same convention as issue creation: the assignee resolves to the GitHub user whose token the `gh` CLI is authenticated as (the project owner running the workflow). Unassigned PRs are treated as drive-by; Ralph and the review pipeline expect a named owner.
  ```sh
  gh pr create -R MarcosACH/tickwright --assignee @me --title "..." --body "..."
  ```
  For PRs already opened without an assignee, use `gh pr edit <n> -R MarcosACH/tickwright --add-assignee @me`.
- **Linked issue on the project board**: a PR's `Closes #<N>` issue must be on project 2 (see **Conventions** → *Create an issue*). `gh issue create` does **not** add it, so any issue you file to back a PR — including a one-off `infra`/`bug` issue created just to satisfy the `Closes` gate — must be added explicitly with `gh project item-add 2 --owner MarcosACH --url <issue-url>` and given a Status (**In Review** once its PR is open). An issue that closes a PR but never reached the board is invisible to triage.

## Linking PRs to issues

Every implementation PR must include `Closes #<issue-number>` in its body. GitHub then:

- Links the PR to the issue (visible from both)
- Auto-closes the issue when the PR merges to the default branch — no manual `gh issue close` needed
- The "Item closed" project automation flips Status to `Done` when the PR merges

Each child PR's `Closes` clause references **its own child slice issue**, never the parent PRD — closing the parent on a child merge would kill the tracking issue while slices are still open. A **parent PRD** is an epic with no merge event of its own: its completion is an *aggregate* condition — every sub-issue Done **and** the release cut — that no `Closes #N` keyword can express. So a PRD is **not** auto-closed; it is closed **deliberately at release**, once every sub-issue is Done, by the maintainer (or a future close-parent automation), commenting the release URL. This is a sanctioned exception to "never close manually" — one of two, the other being a wayfinder map or ticket, which likewise has no merge event of its own (see [Wayfinding operations](#wayfinding-operations)). What the rule targets is **child slice issues**: those always close on their own PR's merge, never by hand. The release-time step and command live in [versioning.md → Parent PRD issues at release](../workflow/versioning.md#parent-prd-issues-at-release).

## Issue lifecycle for agents

The project board has Status column `Backlog → Todo → In Progress → In Review → Done`. Agents drive these transitions explicitly except for the final `Done`, which is automated on merge.

| Trigger                                | Agent action                                                              |
| -------------------------------------- | ------------------------------------------------------------------------- |
| Issue filed by `to-spec` / `to-tickets` | Status set to `Todo` immediately (skips `Backlog`)                       |
| Item intentionally deferred            | Human drags Status to `Backlog`                                           |
| Picking up an issue                    | Remove `needs-triage` if present; move Status to `In Progress`            |
| Opening the PR (with `Closes #<n>`)    | Move Status to `In Review` — signals the ticket is awaiting review        |
| Reviewer approves and merges PR        | Nothing manual. Issue auto-closes; Status auto-moves to `Done` on close   |

`Backlog` is a human-only hold state. Ralph's picker filters for `Status == Todo`, so items in `Backlog` are intentionally skipped until promoted. Never manually close the issue; the merged PR does it.

## Ralph PR lifecycle

Ralph uses PR labels as the deterministic state machine.

| Event                           | New state label            |
| ------------------------------- | -------------------------- |
| PR opened after architecture    | `ralph:needs-review`       |
| Code review blocks merge        | `ralph:changes-requested`  |
| Fixer addresses review findings | `ralph:review-addressed`   |
| Re-review passes merge gate     | `ralph:ready`              |

Set exactly one `ralph:*` label per PR, clearing the others, e.g.:

```sh
gh pr edit <PR> -R MarcosACH/tickwright \
  --add-label ralph:needs-review \
  --remove-label ralph:changes-requested,ralph:review-addressed,ralph:ready
```

Every open Ralph PR must have exactly one `ralph:*` state label; a PR with zero or multiple state labels is a drift bug to fix by hand. **Dependabot PRs are the exception**: the `pr-policy` check skips its `ralph:*`-label and `Closes #N` gates for `dependabot[bot]` (see `docs/workflow/labels.md` and `.github/workflows/pr-policy.yml`), so a dependency PR needs neither.

Provision required labels (one-shot, idempotent):

```sh
.agents/tools/ensure-labels.sh
```

### Moving Status with `gh`

The Status field on the project needs three IDs (project, field, option). Constants for the project (`https://github.com/users/MarcosACH/projects/2`):

```sh
PROJECT_NUMBER=2
PROJECT_ID=PVT_kwHOCO3Woc4BcODC
STATUS_FIELD=PVTSSF_lAHOCO3Woc4BcODCzhW4djE

# Status option ids
BACKLOG=2ca48215
TODO=5afbb6d2
IN_PROGRESS=fb92732f
IN_REVIEW=b35df904
DONE=4b33f99e
```

Find the project item id for an issue and update the field:

```sh
ISSUE=92                                    # the issue number
ITEM_ID=$(gh project item-list "$PROJECT_NUMBER" --owner MarcosACH --format json --limit 200 \
  | jq -r ".items[] | select(.content.number==$ISSUE) | .id")

gh project item-edit \
  --project-id "$PROJECT_ID" \
  --id "$ITEM_ID" \
  --field-id "$STATUS_FIELD" \
  --single-select-option-id "$IN_PROGRESS"
```

If the option ids ever drift (e.g. someone renames a Status option in the GitHub UI), re-discover them with:

```sh
gh api graphql -f query='query{node(id:"'"$STATUS_FIELD"'"){... on ProjectV2SingleSelectField{options{id name}}}}'
```

## Wayfinding operations

Used by [`/wayfinder`](../../.claude/skills/wayfinder/SKILL.md) to plan a huge, foggy effort as a **map** issue with **decision-ticket** child issues. These reuse the mechanics above (project board, sub-issue linking, native dependencies, Status transitions) — this section only says how `/wayfinder` composes them. Labels are provisioned by `.agents/tools/ensure-labels.sh` (`wayfinder:map`, `wayfinder:research`, `wayfinder:prototype`, `wayfinder:grill-with-docs`, `wayfinder:task`; canonical in [`labels.md`](../workflow/labels.md)).

- **Map**: a single issue labelled `wayfinder:map`, holding the Destination / Notes / Decisions-so-far / fog body. Create it like any issue (title, body, `--assignee @me`), add it to project 2, and set Status `Todo` — same flow as *Create an issue* above, just with the `wayfinder:map` label. It is the canonical artifact; its child tickets hold the detail.
- **Child ticket**: an issue linked to the map as a real **GitHub sub-issue** (the `sub_issue_id` REST call in *Link as a sub-issue of a parent* above), added to project 2 at Status `Todo`, carrying one `wayfinder:<type>` label. Its body is a single `## Question`.
- **Blocking**: GitHub's **native issue dependency** (`dependencies/blocked_by`, the `issue_id` = blocker **database id** — see *Link a blocker* above). This is what renders the frontier visually in GitHub's UI. `/wayfinder` does **not** use the `blocked` label here (that label is the Ralph picker's filter; the wayfinder frontier is computed from dependencies + Status, not picked by Ralph).
- **Claim — solo-repo caveat**: assigning a ticket would be the natural claim signal (unassigned = unclaimed), but that can't work here — this repo assigns **every** issue to `@me` at creation (the Assignee convention above), so the assignee never distinguishes claimed from unclaimed. Instead, **claim a wayfinder ticket by moving its Status `Todo → In Progress`** (the *Moving Status with `gh`* recipe above) before any work. Concurrent sessions are rare in a solo repo, so this is a light convention, not a lock.
- **Frontier query**: the map's open child issues whose blockers are all closed and whose Status is still `Todo` (unclaimed); first in map order wins. List the map's sub-issues, drop any with an open `blocked_by`, drop any already `In Progress`:
  ```sh
  gh api repos/MarcosACH/tickwright/issues/<map>/sub_issues --jq '.[] | select(.state=="open") | .number'
  # for each, an open blocker disqualifies it:
  gh api repos/MarcosACH/tickwright/issues/<child>/dependencies/blocked_by --jq '[.[] | select(.state=="open")] | length'
  ```
  (Read each candidate's project Status via the `item-list` query in *Moving Status* to skip the `In Progress` ones.)
- **Resolve**: `gh issue comment <n> --body "<answer>"`, then `gh issue close <n>` (its Status auto-moves to `Done` on close — the same automation as a merged PR), then append a one-line context pointer (gist + link) to the map's Decisions-so-far. A wayfinder ticket closes on **resolution**, not via a PR `Closes #N`; it has no code merge of its own, so this deliberate close is legitimate (it is not a child slice issue — the "never close manually" rule targets those).
- **Land**: when the frontier empties, the effort's research notes move onto `main` in one docs-only PR and every `blob/<branch>/…` URL in the map body and in the research tickets' resolution comments is repointed at a `main` path. Edit the map body with `gh issue edit <map> --body-file`; correct a resolution comment by **appending** a new comment rather than editing the original, so the record of what was answered when stays intact. Procedure and the verbatim-bodies rule are canonical in the [`wayfinder` skill](../../.claude/skills/wayfinder/SKILL.md), *Work through the map* step 6.
- **Close the map**: once the destination's artifact exists and the notes have landed, the map is done — `gh issue comment <map>` then `gh issue close <map> --reason completed` (Status auto-moves to `Done`, as for a ticket). Close it on the **map's own** lifecycle, not the effort's: a map whose destination was a PRD closes when that PRD is filed and sliced, and the PRD then tracks execution on its own (it is the one issue closed deliberately at release — see [`versioning.md`](../workflow/versioning.md)). Leaving an emptied map open is board noise — it advertises a frontier that no longer exists. The closing comment is the part that matters: name where the effort continues — the PRD, the module map, the slice range — because ADRs cite the map for *why* a decision went the way it did, and a reader arriving from one must not dead-end. The map body itself stays untouched; it is the readable record. Like a ticket, a map has no code merge of its own, so this deliberate close is legitimate — the "never close manually" rule targets child slice issues. Worked example: [#107](https://github.com/MarcosACH/tickwright/issues/107).
