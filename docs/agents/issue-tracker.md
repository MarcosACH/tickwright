# Issue tracker: GitHub

Issues and PRDs for Tickwright live as GitHub issues in a **single repository** — `MarcosACH/tickwright`. Use the `gh` CLI for all operations. All issues are tracked on a single user project board: **Tickwright — Workflow** (`https://github.com/users/MarcosACH/projects/<n>`).

## Issue routing

- **Parent PRDs** and **child slice issues** both live in `MarcosACH/tickwright`.
- A PRD breaks into one or more child vertical-slice issues, all linked as GitHub sub-issues of the parent.
- Every slice is a vertical tracer through the relevant layers (event bus → strategy → exchange adapter → reconciliation), filed as its own child issue under the PRD.

## Conventions

- **Create an issue**: `gh issue create -R MarcosACH/tickwright --title "..." --body "..."`. Use a heredoc for multi-line bodies. **`gh issue create` does NOT add the issue to the project board** — you must add it explicitly with `gh project item-add <n> --owner MarcosACH --url <issue-url>` immediately after creation. New project items have no Status until an agent sets one; the `to-prd` and `to-issues` skills set newly-filed issues to `Todo`. Use `Backlog` only for items you want to defer (noted but not yet ready to be picked). An issue that is not on the project board is invisible to triage and Ralph; treat creation-without-linking as a bug.
- **Assignee**: every newly-filed issue (parent PRDs and child slices) must have an assignee set immediately after creation. The convention is `@me` — the GitHub user whose token the `gh` CLI is authenticated as (the project owner running the workflow). `gh issue create --assignee @me` works for the create step; for issues already created, use `gh issue edit <n> -R MarcosACH/tickwright --add-assignee @me`. Unassigned issues are treated as triage-pending and Ralph will not pick them up.
- **Issue Type**: every newly-filed issue must have a GitHub Issue Type set. The repo has three enabled types — `Feature`, `Task`, `Bug`. Convention by source:
  - **PRDs** (filed by `to-prd`) → `Feature`
  - **Child slice issues** (filed by `to-issues`) → `Task`
  - **Bug reports** → `Bug`

  `gh issue create` does NOT support `--type` directly; set it via REST API right after creation:

  ```sh
  gh api -X PATCH repos/MarcosACH/tickwright/issues/<n> -f type=Feature
  ```

  **Important**: omit the leading slash on the endpoint argument. The no-leading-slash form works on every shell.
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
- **Read an issue**: `gh issue view <n> -R MarcosACH/tickwright --comments`.
- **List issues**: `gh issue list -R MarcosACH/tickwright --state open --json number,title,body,labels --jq '[.[] | {number, title, labels: [.labels[].name]}]'`.
- **Comment**: `gh issue comment <n> -R MarcosACH/tickwright --body "..."`.
- **Apply / remove labels**: `gh issue edit <n> -R MarcosACH/tickwright --add-label "..."` / `--remove-label "..."`.
- **Close**: never manually. The PR's `Closes #N` does it on merge.

## When a skill says "publish to the issue tracker"

Create a GitHub issue in `MarcosACH/tickwright`, then add it to the project board. See [`to-issues`](../../.claude/skills/to-issues/SKILL.md) for the full creation flow.

## When a skill says "fetch the relevant ticket"

Run `gh issue view <number> -R MarcosACH/tickwright --comments`.

## Creating PRs

- **Assignee**: every PR opened by a workflow skill (currently `/improve-codebase-architecture` — see [skill section 4](../../.claude/skills/improve-codebase-architecture/SKILL.md)) must set `--assignee @me`. Same convention as issue creation: the assignee resolves to the GitHub user whose token the `gh` CLI is authenticated as (the project owner running the workflow). Unassigned PRs are treated as drive-by; Ralph and the review pipeline expect a named owner.
  ```sh
  gh pr create -R MarcosACH/tickwright --assignee @me --title "..." --body "..."
  ```
  For PRs already opened without an assignee, use `gh pr edit <n> -R MarcosACH/tickwright --add-assignee @me`.

## Linking PRs to issues

Every implementation PR must include `Closes #<issue-number>` in its body. GitHub then:

- Links the PR to the issue (visible from both)
- Auto-closes the issue when the PR merges to the default branch — no manual `gh issue close` needed
- The "Item closed" project automation flips Status to `Done` when the PR merges

Each child PR's `Closes` clause references the parent PRD issue.

## Issue lifecycle for agents

The project board has Status column `Backlog → Todo → In Progress → In Review → Done`. Agents drive these transitions explicitly except for the final `Done`, which is automated on merge.

| Trigger                                | Agent action                                                              |
| -------------------------------------- | ------------------------------------------------------------------------- |
| Issue filed by `to-prd` / `to-issues`  | Status set to `Todo` immediately (skips `Backlog`)                        |
| Item intentionally deferred            | Human drags Status to `Backlog`                                           |
| Picking up an issue                    | Remove `needs-triage` if present; move Status to `In Progress`            |
| Opening the PR (with `Closes #<n>`)    | Move Status to `In Review` — signals the ticket is awaiting review        |
| Reviewer approves and merges PR        | Nothing manual. Issue auto-closes; Status auto-moves to `Done` on close   |

`Backlog` is a human-only hold state. Ralph's picker filters for `Status == Todo`, so items in `Backlog` are intentionally skipped until promoted. Never manually close the issue; the merged PR does it.

## Ralph PR lifecycle

Ralph uses PR labels as the deterministic state machine.

| Event                           | Command                                                                    |
| ------------------------------- | -------------------------------------------------------------------------- |
| PR opened after architecture    | `.ralph/set-pr-state.sh <PR> needs-review`                                 |
| Code review blocks merge        | `.ralph/set-pr-state.sh <PR> changes-requested`                            |
| Fixer addresses review findings | `.ralph/set-pr-state.sh <PR> review-addressed`                             |
| Re-review passes merge gate     | `.ralph/set-pr-state.sh <PR> ready`                                        |

Every open Ralph PR must have exactly one `ralph:*` state label. Run `.ralph/doctor.sh` to inspect drift (the "Invalid PR Label States" and "Slice Coherence" sections surface mismatches).

Provision required labels (one-shot, idempotent):

```sh
.ralph/ensure-labels.sh
```

### Moving Status with `gh`

The Status field on the project needs three IDs (project, field, option). Constants for the project:

```sh
PROJECT_ID=<project-node-id>
STATUS_FIELD=<status-field-id>

# Status option ids
BACKLOG=<backlog-option-id>
TODO=<todo-option-id>
IN_PROGRESS=<in-progress-option-id>
IN_REVIEW=<in-review-option-id>
DONE=<done-option-id>
```

Find the project item id for an issue and update the field:

```sh
ISSUE=92                                    # the issue number
ITEM_ID=$(gh project item-list <n> --owner MarcosACH --format json --limit 200 \
  | jq -r ".items[] | select(.content.number==$ISSUE) | .id")

gh project item-edit \
  --project-id "$PROJECT_ID" \
  --id "$ITEM_ID" \
  --field-id "$STATUS_FIELD" \
  --single-select-option-id "$IN_PROGRESS"
```

If the option ids ever drift (e.g. someone renames a Status option in the GitHub UI), re-discover them with:

```sh
gh api graphql -f query='query{node(id:"<status-field-id>"){... on ProjectV2SingleSelectField{options{id name}}}}'
```
