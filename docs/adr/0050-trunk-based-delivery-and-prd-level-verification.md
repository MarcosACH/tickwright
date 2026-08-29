# Trunk-based delivery: every slice merges to main, PRD assurance at the tail and the release

The workflow delivers a PRD as N vertical slices, each its own branch, each merged on its own green
review (`CLAUDE.md` → *PR policy*). That raises a fair question: a slice is verified, but the **PRD**
— the thing the user actually asked for — is only ever assembled *on `main`*, one merge at a time,
and nothing verifies the assembled whole before it lands there. The obvious fix is a shared PRD
integration branch that every child merges into, verified once and fast-forwarded to `main`.

This ADR rejects that, and fixes where PRD-level assurance lives instead.

## Every implementation PR targets the default branch

`main` is the integration branch. There is no long-lived per-PRD or per-epic branch, and a slice is
not held back waiting for its siblings.

The premise that makes this safe is that an unlanded slice is **absent from `main`, not broken on
it**. The vertical-slice rule already demands each slice be independently demoable, and the
config surface carries the same discipline explicitly: a variable is *specified-but-not-yet-wired*
until the slice that reads it lands (`CLAUDE.md` tracks this variable by variable). A half-delivered
PRD is therefore a `main` that does less than the PRD promises — never a `main` that does it wrongly.
Half-delivered is the steady state, not an exception: PRD #168 sat at 16 of 27 slices closed with
`main` releasable throughout.

## Rejected: a shared PRD integration branch

It would break the mechanism the whole workflow is built on. GitHub interprets closing keywords
**only** when a pull request targets the default branch; against any other base *"these keywords are
ignored, no links are created, and merging the PR has no effect on the issues"*. Three things fail at
once, all of them load-bearing:

- `Closes #<N>` stops auto-closing the slice issue (`docs/agents/issue-tracker.md` → *Linking PRs to
  issues*), so children would need the manual `gh issue close` that the same document forbids.
- The *Item closed* project automation never fires, so Status never reaches `Done`.
- The auto-transition to **In Review** never fires either — it keys on the PR↔issue link, and no link
  is created at all.

The failure is worse than losing the automation, because `pr-policy.yml`'s *Body closes an issue*
step greps the PR body and **would still pass**. The ceremony reports green while the effect
silently does not happen — the same shape as a test suite that skips and reports success.

The mechanical costs compound it. Both rulesets target `~DEFAULT_BRANCH`, so equivalent protection
would have to be duplicated onto a branch pattern and kept in sync. `protect-main` sets
`strict_required_status_checks_policy` with merge-commit-only, so a long-lived branch needs
continuous restacking against a `main` that keeps moving, while Dependabot — which targets the
default branch only — drifts it further on every dependency bump. And the eventual PRD→`main` pull
request is an N-slice diff that no reviewer can meaningfully read, which discards the per-slice
review that was the point of slicing.

**What we accept in exchange:** `main` can contain a PRD's first half without its second. That is
made safe by absence-not-breakage above, and verified by the three gates below.

## Where PRD-level assurance actually lives

**1. An `integrate-and-verify` slice ends every PRD.** The last child, blocked by all its siblings,
whose acceptance criteria are the cross-slice scenarios no single slice could assert — the
end-to-end path, restart recovery across the whole surface, and the same scenario through each store
backend. It is an ordinary slice on an ordinary branch, so `Closes #<N>` and the board keep working.
`.claude/skills/to-tickets/SKILL.md` step 3 makes it standard.

**2. The merge gate covers every hermetic path**, not just the default one — including the
`PostgresStore` contract, so the ADR-0019 parity promise is gated rather than merely available
(issue #253). A gate that silently skips a supported backend cannot support this ADR's premise.

**3. The release is the aggregate checkpoint.** A PRD closes deliberately at release, once every
sub-issue is Done (`docs/workflow/versioning.md`), and the pre-tag checks include the non-hermetic
`ci-live` run (issue #255). This is the point at which "the PRD is delivered" is asserted about the
assembled whole rather than about any one slice.

## The one sanctioned integration branch

A **wide refactor** whose migrate batches cannot each stay green alone keeps the shared integration
branch and its final integrate-and-verify slice — `to-tickets` already defines this as the sanctioned
way to break the vertical-slice policy, for blast radius that genuinely forces it. Its children pay
the cost this ADR describes: their issues close by hand, because GitHub will not close them. Say so
in the slice bodies, and scope a ruleset to the branch pattern so the checks still gate.

## Consequence for a second contributor

Trunk-based scales *better* with a second person, not worse. A shared long-lived branch is precisely
where two developers collide — both restacking it, `dismiss_stale_reviews_on_push` firing on every
update. What does change with a contributor is already provisioned and needs no branching decision:
`require-review` demands one approving review plus a code-owner review, which the maintainer bypasses
today as repository admin.
