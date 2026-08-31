# Skill evals

The third test tier. `ci` proves the **engine** still behaves; this proves the **skills in
`.claude/skills/` still say what they are supposed to say**.

Those skills carry mandatory policy: `/tdd` enforces red-before-green and seam confirmation,
`/unslop` enforces the writing rules, `/code-review` enforces the BLOCKING/WARN/NIT gate. Nothing
in `ci` reads them. Edit a skill's wording and the rule can stop landing with no failing check and
no diff that looks wrong. An eval is the only test that notices.

Runner: `claude plugin eval` (Claude Code CLI). See *Availability* below before you plan on it.

## Why a case has two arms

The whole point is the **ablation**: every case runs twice, once with the skill loaded and once
without, and reports the score delta. A case that scores the same in both arms is not testing the
skill, it is testing the base model. That case is worthless and the delta is what tells you so.

This is the default (`--ablation with-without`) whenever a plugin resolves. Graders marked
`arm: with-only` (the `tool_used: Skill` firing check) are reported as a plugin-fired indicator
rather than counted in the score, because the without-arm cannot fire a skill it does not have.

## Layout

```
evals/
  README.md                       this file
  <skill>/<case-name>/case.yaml   one case, one behavior
  results/                        run output, gitignored
```

Discovery is `evals/**/case.yaml`, so the directory names are yours to pick. One case asserts one
behavior. A case with eight graders is a case whose failure tells you nothing.

Each case names the skill it exercises in `plugins:`, as a path under the repo root. That is what
resolves the with-arm. The repo root has no plugin manifest, so without `plugins:` both arms would
run identical and the ablation would be meaningless.

## Running

```bash
claude plugin eval .                       # every case, both arms
claude plugin eval . --case 'tdd-*'        # one case by name glob
claude plugin eval . --tag tdd             # by tag
claude plugin eval . --runs 1              # cheap smoke pass
claude plugin eval . --model <model>       # override the agent under test
```

Run it from the repo root. Results land in `evals/results/<timestamp>/` with a self-contained HTML
report.

**These cases have never been run.** They are written to the validated schema, but a schema-valid
case can still be a badly calibrated one. The first run is the acceptance step: expect to retune
grader criteria against what the model actually produces, and treat a case that passes in both arms
as a bug in the case.

### Cost

Agent runs and LLM graders both cost money, and `runs` defaults to 3 across 2 arms, so one case is
6 agent runs. Keep `max_turns` low, prefer free graders (`regex`, `tool_used`, `file_exists`) over
`llm`, and bound a batch with `--max-cost-usd`. The judge model defaults to haiku, overridable with
`--judge-model`.

This tier is **not** a merge gate and should not become one. Scores are noisy across runs, the
runner reaches the API, and a check that goes red for reasons unrelated to the diff trains its
reader to ignore it. Same reasoning as `ci-live` in
[`.github/workflows/ci-live.yml`](../.github/workflows/ci-live.yml). Run evals when you edit a
skill, and before a release.

### Availability

`claude plugin eval` is in **early access, enabled per organization**. It is not enabled in this
repo's default environment yet: the command exists and prints `` `plugin eval` is currently in
early access `` instead of running. An enablement variable exists for machines that cannot receive
the per-organization rollout (CI runners, gateways, telemetry-disabled clients). Get the current
name from your Anthropic contact rather than copying one from anywhere, including here. Until then
the cases sit as reviewed intent.

## Case format

`case.yaml`, validated against the runner's schema (`schema_version: "1.0"`):

| key | meaning |
| --- | --- |
| `schema_version` | required, `"1.0"` |
| `name` | required, unique. What `--case` globs against |
| `description` | what the case asserts, in one sentence |
| `tags` | what `--tag` filters on |
| `plugins` | paths to the skill directories under test |
| `context.add_dirs` | directories mounted into the sandbox. Empty means an empty sandbox |
| `context.scaffold_script` | setup bash. Runs only under `--scaffold`, as you, so never on a case you did not write |
| `execution.prompt` | the user turn. Omit to use a sibling `prompt.md` |
| `execution.max_turns` | default 10, max 200 |
| `execution.timeout_seconds` | default 300, max 3600 |
| `execution.allowed_tools` | tools the case asks for. `Bash`, `Write`, `Edit`, `WebFetch` and MCP tools additionally need the operator grant `--allow-tools` |
| `runs` | repeats per arm, default 3, max 50 |
| `graders` | at least one, names unique within the case |

## Graders

Every grader takes `name`, optional `weight` (positive, default 1) and optional
`arm` (`with-only` | `both`). A run's score is the weighted fraction of its graders that passed.

| `type` | fields | cost |
| --- | --- | --- |
| `regex` | `target`, `pattern`, `flags` (JS RegExp flags), `match`: `contains` \| `not_contains` \| `count:N` | free |
| `tool_used` | `tool`, `input_match`, `min`, `max` | free |
| `tool_order` | `before`, `after`, each a tool name or `{tool, input_match}` | free |
| `file_exists` | `path`, `exists` (default true) | free |
| `llm` | `criteria`, `focus` | paid |
| `baseline` | `baseline_file`, `criteria` | paid |

`target` and `focus` take `trace`, `last_message`, `files`, `mock_calls`, or
`{source: file, path: <path>}`. Both default to `last_message`.

Reach for `llm` only where the claim is genuinely about prose. Judges are noisy on long inputs, and
a `regex` over a planted phrase is both free and stable.

## What these cases cover, and what they do not

Cases come in two shapes. The cheap one runs in an **empty sandbox with no write tools** and grades
what the skill tells the agent to *do* (`tdd/red-before-green`); the expensive one mounts a fixture
tree, allows `Write`/`Edit`/`Bash`, and grades the edits themselves
(`tdd/writes-test-before-src`, `tdd/edits-instead-of-bash-writes`). A skill is guidance, and
guidance that stopped being given is the failure worth catching — but only the second shape can
catch guidance that is given and then not followed.

Neither shape measures **token consumption**. The context-budget rules in `/tdd` are graded by
proxy — which tool was used, whether a doc was sliced or read whole, whether reading was deferred
past the first test — because a token threshold drifts with the model version and reads as a
regression when nothing regressed. Assert behavior, not a number.

### Fixture conventions

- A fixture lives at `<case>/fixture/` and is named in `context.add_dirs`, mounted at the sandbox
  root — so paths in prompts and `input_match` are `src/...`, not `fixture/src/...`. Unverified;
  see the note in `tdd/writes-test-before-src/case.yaml`.
- Fixtures carry no packaging. A `conftest.py` putting `src/` on `sys.path` is what stands in for
  `pip install -e .`.
- A fixture may carry a path the repo gitignores — `tdd/resumes-from-plan/fixture/.agents/plans/`
  is committed, because `.gitignore`'s `.agents/plans/` has an interior slash and is therefore
  anchored to the repo root. Check a new one with `git check-ignore -v <path>` before assuming it
  survives the commit.
- Copy a tool the case needs into the fixture rather than reaching out of the sandbox for it
  (`tdd/plans-with-sliced-reading/fixture/.agents/tools/doc-slice`). It drifts, and a stale copy is
  the cost of a hermetic sandbox.

### Grading a written artifact

`regex` with `target: {source: file, path: <path>}` reads a file the run produced, so a claim about
an artifact's *contents* stays free. `file_exists` proves it was written at all. Reach for `llm`
only where the claim is genuinely about prose — `tdd/writes-plan-artifact` proves the plan exists
and then greps it, and pays for neither.

## Adding a case

1. Pick one behavior a skill is supposed to produce, phrased so a failure names the bug.
2. Write the prompt as a user would type it. For a skill with `disable-model-invocation: true`
   (`/blast-radius`, `/grill-with-docs`, `/wayfinder` and others) the prompt must invoke it by
   `/name`, because the model cannot reach it on its own.
3. Add a `tool_used: Skill` grader with `arm: with-only` so a case that stops firing the skill is
   legible rather than just low-scoring.
4. Add the behavior graders. Free ones first.
5. Run it. Confirm it fails when you delete the relevant lines from the skill. A case that passes
   against a gutted skill is not a test.
