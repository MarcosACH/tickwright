# Versioning, Tags & Releases

Canonical policy for how Tickwright is versioned, tagged, and released. Follow it for every release, human or agent.

We use [Semantic Versioning 2.0.0](https://semver.org/) (`MAJOR.MINOR.PATCH`). Versioning is **static**: the version lives in `pyproject.toml` and the git tag mirrors it by hand. There is no version-derivation tooling (`hatch-vcs` was considered and deliberately deferred — see [Static versioning, not VCS-derived](#static-versioning-not-vcs-derived)).

## When to propose a release — and who decides

A release is a **deliberate, maintainer-approved cut**, never automatic and never self-authorized by an agent. Two duties follow:

- **Propose proactively.** Whenever work reaches a coherent, shippable milestone — a PRD delivered, a meaningful feature set landed, an important fix merged — surface it: recommend the version number *with its SemVer rationale* (why patch vs. minor vs. major, per the rules below) and the scope it covers. Don't let releasable work sit untagged, and don't tag without proposing.
- **Get sign-off before tagging.** The version *number* is an outward-facing promise (a `MINOR` may signal a breaking `0.x` change; a `MAJOR` or the `0.x → 1.0.0` step is a stability commitment). That call is the maintainer's — propose and wait for approval; do not cut the tag or Release on your own judgment.

## The public API this versions

SemVer is a promise about a *public API*. For Tickwright that surface is:

- the **seam Protocols** a user implements or swaps — `Strategy`, `MarketFeed`, `Exchange`, `EventBus`, `Store`, `Clock`, fill models (ADR-0032);
- the **config contract** — `AppConfig` / the `TICKWRIGHT_*` environment variables (`.env.example` is canonical);
- the **CLI** — `tickwright` / `python -m tickwright.app`.

Internal engine mechanics (saga internals, reconciler scheduling, private modules) are **not** part of the versioned contract; changing them is not a breaking change.

## Current phase: `0.x` (initial development)

The project is at major version zero. Per SemVer, `0.y.z` means **the public API is not yet declared stable — anything MAY change**. Concretely, while on `0.x`:

- **`0.MINOR.0`** (`0.2.0`, `0.3.0`, …) — any feature work or a change that *may* break the seam/config/CLI contract. Minor is the "might-break" lane in `0.x`; no ceremony beyond release notes.
- **`0.y.PATCH`** (`0.1.1`, …) — bug fixes with no contract change.

`0.1.0` is the first release: the complete v1 core-engine scope of PRD #9.

### Reaching `1.0.0`

Cut `1.0.0` only when the seam/config/CLI contract is one **users can depend on** — for Tickwright, when it is confirmed ready for real-money use and any changes that confirmation demands have landed. `1.0.0` is a stability *promise*; do not make it before you can keep it.

### After `1.0.0`

Versions only ever move **forward** — you can never return to `0.x`. Instability is then expressed precisely, not by a low major:

- **breaking** change to the public API → **`MAJOR`** bump (`2.0.0`). Known future scope — backtesting, fees/margin, multi-venue-in-process — is breaking-by-nature and lands as a new major.
- backward-compatible **feature** → **`MINOR`** (`1.1.0`).
- backward-compatible **fix** → **`PATCH`** (`1.0.1`).
- need to ship a not-yet-stable build → **pre-release tag** (`1.1.0-rc.1`), which sorts *below* the final `1.1.0`.

## Static versioning, not VCS-derived

`pyproject.toml`'s `version` is the **source of truth** — it is what `uv build` / `hatchling` stamp into wheel and sdist filenames and metadata, what a consumer's `tickwright>=…` resolves against, and what `importlib.metadata.version("tickwright")` returns once installed. The package's runtime `tickwright.__version__` is *derived* from that installed metadata (`src/tickwright/__init__.py` calls `importlib.metadata.version("tickwright")`, falling back to `"0.0.0+unknown"` in an uninstalled raw checkout), so there is no second hardcoded string to bump — `pyproject.toml` is the only in-repo copy (issue #97). **The git tag and `pyproject.toml` are two independent records kept in sync by hand.** Tagging does not read or change `pyproject.toml`; `pyproject.toml` does not know a tag exists. If they drift, built artifacts are mislabeled — a real bug, not cosmetic.

Consequence for the procedure below: **bump the file first, then tag the commit that carries the bump.** (`0.1.0` needed no bump — the scaffold already declared it — so its release was tag-only.)

> **Deferred: VCS-derived versioning.** `hatch-vcs` would make the git tag the single source of truth (version computed at build time, removed from `pyproject.toml`), eliminating drift and the manual bump. It is **not** adopted while the repo publishes no artifacts; revisit when a PyPI or automated-wheel publish step is added — that is when manual bumps and drift become real friction. Its one gotcha: builds need full git history (`actions/checkout` with `fetch-depth: 0`).

## Release procedure

A tag and a GitHub Release are metadata pointing at an existing commit, **not** a commit to `main` — so the **tag itself** needs no PR and does not touch the PR-only / `Closes #N` branch protection (the `## History` entry it must capture still rides a PR — see step 1). A release that *does* bump `pyproject.toml` ships that bump as a normal PR first, then tags the merge commit.

1. **(If the number changes)** Bump `version` in `pyproject.toml` in a PR against its own chore issue, **and add this release's [`## History`](#history) entry in the same PR** — so the tag lands on a commit that documents its own release. Merge it. Write the *intended* cut date (this repo tags right after the merge, so it is reliable) and keep the entry to facts known now — scope, SemVer rationale — referencing the driving issue, not the bump PR's own not-yet-assigned number. Skip the bump if the target version already matches; a **tag-only release** (no bump PR, as `0.1.0` shipped) instead adds its History entry in a small *pre-tag* docs PR, since the entry must live in the tagged commit.
2. **Confirm `main` is in sync** with the remote and tag its HEAD — never an earlier "last feature" commit, which would omit merged work:
   ```bash
   git fetch origin main
   git tag -a vX.Y.Z <main-HEAD-sha> -m "Tickwright vX.Y.Z — <one-line scope>"
   git push origin vX.Y.Z
   ```
   Use an **annotated** tag (`-a`) — it carries tagger/date/message and is what Releases and `git describe` expect. Tag names are `v`-prefixed (`v0.1.0`).
3. **Cut the GitHub Release** from that tag, notes summarizing the delivered scope and linking the driving PRD/issues:
   ```bash
   gh release create vX.Y.Z --title "vX.Y.Z — <scope>" --notes-file <notes.md> --latest
   ```
4. **Close the parent PRD** (see next section).

> **Tag creation is ruleset-restricted.** A repository ruleset restricts creating refs under `refs/tags/*`; the maintainer with bypass rights pushes the tag (the push reports a bypassed restriction — expected, not an error).

## Parent PRD issues at release

Child vertical-slice issues auto-close on PR merge (`Closes #N`) — never close them by hand. A **parent PRD**, by contrast, has no merge event of its own, so no `Closes #N` can close it (the rule and its rationale live in [issue-tracker.md → Linking PRs to issues](../agents/issue-tracker.md#linking-prs-to-issues)). At release, once all sub-issues are closed, **close the PRD deliberately**, commenting with the release URL — the one sanctioned exception to "never close issues manually", applying only to parent PRDs, never to child issues.

```bash
gh issue close <PRD#> --comment "Delivered in vX.Y.Z: <release-url>. All sub-issues complete."
```

> **Deferred: close-parent automation.** A tiny GitHub Actions job on the `issues: closed` event — "if the closed issue is a sub-issue and all its siblings are now closed, close the parent, commenting the release URL" — would remove the one manual step. It is **not** adopted yet: it needs the release URL threaded in (the parent closes at *release*, not on the last child's close), so the trigger is really "release cut", not "last child closed". Revisit if forgotten PRD-closes become real friction; until then the deliberate `gh issue close` above is the sanctioned path.

## History

- **v0.1.0** (2026-07-13) — first release; full v1 core-engine scope of PRD #9. Tag-only (scaffold already declared `0.1.0`).
- **v0.2.0** (2026-07-21) — hardening release; the `AppConfig` config-contract split (#71) sets the MINOR floor, alongside git-hook fixes, test-hermeticity gating, workflow docs, and dependency bumps. First bump-PR release (#96 moved `pyproject.toml` `0.1.0` → `0.2.0`), in contrast to `v0.1.0`'s tag-only cut.
