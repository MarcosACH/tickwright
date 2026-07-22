#!/usr/bin/env bash
# ensure-labels.sh — idempotently provision the grill-with-docs label schema.
#
# Canonical source of truth for the schema is docs/workflow/labels.md; keep this
# list in sync with it. Safe to re-run: creates a label if missing, otherwise
# updates its colour/description. GitHub defaults (bug, enhancement, …) are left
# untouched.
#
# Usage:
#   .agents/tools/ensure-labels.sh              # provisions MarcosACH/tickwright
#   REPO=owner/name .agents/tools/ensure-labels.sh
set -euo pipefail

REPO="${REPO:-MarcosACH/tickwright}"

# name|color|description
LABELS=(
  # Process
  "prd|5319e7|Parent PRD issue — sub-issues describe vertical slices"
  "ai|bfd4f2|Created or executed by an AI agent"

  # PR state — review state machine
  "ralph:needs-review|1d76db|PR opened, awaiting code-review"
  "ralph:changes-requested|fbca04|Code-review found BLOCKING/WARN issues; must fix"
  "ralph:review-addressed|f9a825|Fixes pushed; awaiting re-review"
  "ralph:ready|0e8a16|Clean review; eligible for merge"

  # Issue priority
  "tracer|5319e7|Vertical tracer-bullet slice — crosses all relevant layers"
  "critical|b60205|Blocks operation, data integrity, or recovery"
  "infra|0052cc|Infrastructure / tooling / scripts / CI"
  "polish|c5def5|Code-quality polish, lowest priority"

  # Human gates — pause autonomous work
  "needs-triage|d4c5f9|New issue, not yet prioritized"
  "needs-human-validation|fef2c0|Ambiguous; needs human review before AFK work"
  "ready-for-human|fef2c0|Automated part complete; human action required"
  "needs-info|fef2c0|Blocked on a question or external info"
  "blocked|000000|Hard-blocked by an external dependency"

  # Issue domain — heavy labels route to the strongest model
  "recovery|d93f0b|Crash/restart recovery, idempotency, sagas, checkpoints"
  "engine|d93f0b|Core event-bus, order-lifecycle state machine, runner"
  "exchange|d93f0b|Exchange adapter, order placement/cancel, reconciliation"
  "chain|d93f0b|Live Hyperliquid integration, signing, key material"
  "concurrency|d93f0b|Async ordering, races, delivery guarantees"
  "unsafe|d93f0b|Modifies an invariant flagged \"unsafe\" in CONTEXT.md"

  # Wayfinder — decision-ticket maps (used only by the wayfinder skill)
  "wayfinder:map|006b75|The map issue — index of decision tickets for one effort"
  "wayfinder:grill-with-docs|0e8a8f|Decision ticket resolved by /grill-with-docs (default type)"
  "wayfinder:research|2bb0bd|AFK ticket resolved by a /research subagent"
  "wayfinder:prototype|5ec8d3|HITL ticket resolved by a /prototype logic prototype"
  "wayfinder:task|bfe9ee|Manual prerequisite that unblocks a decision (HITL or AFK)"
)

for entry in "${LABELS[@]}"; do
  IFS='|' read -r name color desc <<<"$entry"
  if gh label create "$name" -R "$REPO" --color "$color" --description "$desc" 2>/dev/null; then
    echo "created  $name"
  else
    gh label edit "$name" -R "$REPO" --color "$color" --description "$desc" >/dev/null
    echo "updated  $name"
  fi
done
