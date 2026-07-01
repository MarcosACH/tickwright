# GitHub Label Schema — grill-with-docs Workflow

This is the canonical label set for the grill-with-docs workflow in this **single repo**. Provision it with `.agents/tools/ensure-labels.sh` (idempotent) or `gh label create` directly.

The schema has five concerns: PR state machine, issue priority, issue domain (drives model selection if the AFK loop is used), human gates, and process.

> Single-repo project — there is **no** `repo:*` routing dimension. The parent PRD issue and all child vertical-slice issues live in this one repo.

## PR state — review state machine

Exactly one `ralph:*` label is set on every PR. The transitions are: `ralph:needs-review` → `ralph:changes-requested` ⇄ `ralph:review-addressed` → `ralph:ready`. (The `ralph:` prefix is kept as the conventional review-state namespace even if the AFK loop is not run.)

| Name | Color | Description |
|---|---|---|
| `ralph:needs-review` | `#1d76db` (blue) | PR opened, awaiting code-review |
| `ralph:changes-requested` | `#fbca04` (yellow) | Code-review found BLOCKING/WARN issues; must fix |
| `ralph:review-addressed` | `#f9a825` (orange) | Fixes pushed; awaiting re-review |
| `ralph:ready` | `#0e8a16` (green) | Clean review; eligible for merge |

## Issue priority

| Name | Color | Description |
|---|---|---|
| `tracer` | `#5319e7` (purple) | Vertical tracer-bullet slice — crosses all relevant layers |
| `critical` | `#b60205` (red) | Blocks operation, data integrity, or recovery |
| `infra` | `#0052cc` (deep blue) | Infrastructure / tooling / scripts / CI |
| `polish` | `#c5def5` (light blue) | Code-quality polish, lowest priority |

## Issue domain — drives model selection (only if the AFK loop is adopted)

Issues carrying any **heavy** label route to the strongest model with 1M context. All others route to the default model. The heavy set is the domain labels listed below.

Heavy:

| Name | Color | Description |
|---|---|---|
| `recovery` | `#d93f0b` (dark orange) | Crash/restart recovery, idempotency, sagas, checkpoints |
| `engine` | `#d93f0b` | Core event-bus, order-lifecycle state machine, runner |
| `exchange` | `#d93f0b` | Exchange adapter, order placement/cancel, reconciliation |
| `chain` | `#d93f0b` | Live Hyperliquid integration, signing, key material |
| `concurrency` | `#d93f0b` | Async ordering, races, delivery guarantees |
| `unsafe` | `#d93f0b` | Modifies an invariant flagged "unsafe" in CONTEXT.md |

Light — no label needed; default routing.

## Human gates — pause autonomous work

Any one of these on an issue means it must not be picked autonomously.

| Name | Color | Description |
|---|---|---|
| `needs-triage` | `#d4c5f9` (lilac) | New issue, not yet prioritized |
| `needs-human-validation` | `#fef2c0` (pale yellow) | Ambiguous; needs human review before AFK work |
| `ready-for-human` | `#fef2c0` | Automated part complete; human action required |
| `needs-info` | `#fef2c0` | Blocked on a question or external info |
| `blocked` | `#000000` (black) | Hard-blocked by an external dependency |

## Process

| Name | Color | Description |
|---|---|---|
| `prd` | `#5319e7` (purple) | Parent PRD issue — sub-issues describe vertical slices |
| `ai` | `#bfd4f2` (sky) | Created or executed by an AI agent |
| `bug` | `#d73a4a` (red) | (Existing GitHub default; preserved) |

---

## Provisioning

Single repo, so provisioning is one pass:

```bash
.agents/tools/ensure-labels.sh     # idempotent; provisions the whole schema
# or, one label directly:
gh label create tracer --color 5319e7 --description "Vertical tracer-bullet slice" || \
  gh label edit tracer --color 5319e7 --description "Vertical tracer-bullet slice"
```

Make provisioning idempotent: wrap each `gh label create` so it updates colour/description if the label already exists and creates it otherwise. Do not touch existing GitHub defaults (`enhancement`, `documentation`, etc.).
