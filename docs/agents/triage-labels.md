# Triage Labels

Canonical role → label mapping for Tickwright. The full label catalog with colors and descriptions lives in [`docs/workflow/labels.md`](../workflow/labels.md); this file is the role lookup used by skills.

| Canonical role            | Label string in this tracker | Meaning                                       |
| ------------------------- | ---------------------------- | --------------------------------------------- |
| `needs-triage`            | `needs-triage`               | Maintainer needs to evaluate this issue       |
| `needs-info`              | `needs-info`                 | Waiting on reporter for more information      |
| `ready-for-agent`         | `ai` + one priority label, **no** human-gate labels | Fully specified, ready for an AFK Ralph iteration |
| `ready-for-human`         | `ready-for-human`            | Requires human implementation                 |
| `needs-human-validation`  | `needs-human-validation`     | Maintainer must validate before Ralph starts  |
| `blocked`                 | `blocked`                    | Hard-blocked by an external dependency        |

When a skill mentions a role (e.g. "apply the AFK-ready triage label"), use the corresponding label string from this table. Tickwright does **not** use a single `ready-for-agent` label — the absence of human-gate labels combined with `ai` + a priority label is the signal that Ralph treats as "ready."

## Ralph PR State Labels

Each open Ralph PR must have exactly one state label. The transitions and commands are documented in [`issue-tracker.md`](./issue-tracker.md#ralph-pr-lifecycle).

| Label                      | Owner              | Meaning                                      |
| -------------------------- | ------------------ | -------------------------------------------- |
| `ralph:needs-review`       | reviewer agent     | PR is waiting for `code-review`              |
| `ralph:changes-requested`  | fixer agent        | Latest review blocks merge                   |
| `ralph:review-addressed`   | reviewer agent     | Findings were fixed; re-review needed        |
| `ralph:ready`              | human maintainer   | Merge gate passed; waiting for human merge   |

Set these with `gh pr edit` — exactly one at a time, clearing the others (see [issue-tracker.md](./issue-tracker.md#ralph-pr-lifecycle)).

## Priority Labels

Every AFK implementation issue created by `to-issues` gets exactly one priority label:

| Label      | Meaning                                                  |
| ---------- | -------------------------------------------------------- |
| `critical` | Blocks operation, data integrity, or recovery            |
| `infra`    | Infrastructure, scaffolding, schemas, shared foundations |
| `tracer`   | Normal vertical implementation slice                     |
| `polish`   | Low-risk cleanup/refinement                              |

## Domain Labels (drive Ralph model selection)

Issues carrying any of these "heavy" labels route to the strongest model (1M context); all others route to the default model. **Canonical source: `docs/workflow/labels.md`** — keep this list in sync with it.

Heavy:

- `recovery` — crash/restart recovery, idempotency, sagas, checkpoints
- `engine` — core event-bus, order-lifecycle state machine, runner
- `exchange` — exchange adapter, order placement/cancel, reconciliation
- `chain` — live Hyperliquid integration, signing, key material
- `concurrency` — async ordering, races, delivery guarantees
- `unsafe` — modifies an invariant flagged "unsafe" in `CONTEXT.md`

## Picker order and intra-PRD ordering

Ralph picks the next `tdd-implementation` candidate as follows:

1. Walk open PRD issues (label `prd`) in the repo, sorted by issue number ascending.
2. For each PRD, walk its open sub-issues in the order returned by GitHub (attachment order).
3. Pick the first sub-issue whose Status is `Todo` and whose labels do **not** include any of `needs-human-validation`, `ready-for-human`, `needs-info`, or `blocked`.
4. Stop at the first match across all PRDs.

There is no priority-label sort within the picker. Priority labels still drive model selection (via the heavy-label set) and serve human triage, but they don't reorder the queue.

### Enforcing intra-PRD ordering

To enforce "slice B must wait for slice A," apply the `blocked` label to slice B. The picker filters `blocked` issues out of the queue. When slice A merges, remove `blocked` from slice B so the next iteration can pick it.

Rule of thumb: when `to-issues` opens slices for a new PRD, label every slice except the first as `blocked`. As each slice merges, unblock the next.

When closing an issue (or merging a PR that closes one), scan open issues labeled `blocked` whose `## Blocked by` section references the just-closed issue. If all their listed blockers are now closed, remove `blocked` from those issues so the picker can pull them.
