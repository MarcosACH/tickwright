# Skill evals

The third test tier. `ci` proves the **engine** still behaves; this proves the **skills in
`.claude/skills/` still say what they are supposed to say**.

Those skills carry mandatory policy: `/tdd` enforces red-before-green and seam confirmation,
`/blast-radius` refuses to sign off without proving its safety fact, `/code-review` enforces the
BLOCKING/WARN/NIT gate. Nothing in `ci` reads them. Edit a skill's wording and the rule can stop
landing with no failing check and no diff that looks wrong. An eval is the only test that notices.

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
(`tdd/writes-test-before-src`). A skill is guidance, and guidance that stopped being given is the
failure worth catching — but only the second shape can catch guidance that is given and then not
followed.

**A rule that becomes a hook stops belonging here.** Two cases used to sit beside those and no
longer exist. `tdd/edits-instead-of-bash-writes` measured whether the skill persuaded a model not to
`sed -i` a file it had read; `.claude/hooks/no-tracked-writes.py` refuses the call outright.
`tdd/plans-with-sliced-reading` measured whether it sliced a module map instead of loading it;
`.claude/hooks/no-unsliced-doc-reads.py` refuses that too, on both the `Read` and the `cat` the case
had to grade separately. Each question is now settled by an exit code rather than by six agent runs
and an LLM grader, and each case was deleted, not kept. Two assertions of one rule is one assertion
too many, and the probabilistic one is the copy that drifts. The deterministic halves are fenced by
`tests/test_claude_hooks.py`, in `ci`, for free.

**Check the scenario before you conclude a hook superseded a case.** `tdd/resumes-from-plan` looked
like the third deletion when `.claude/hooks/resume-from-plan.py` landed — that hook prints the
plan's open behaviors at session start, which is exactly what the case's `plan-consulted` grader
measures the skill persuading a model to go and find. It does not supersede it. The hook derives the
issue number from a `ralph/issue-<N>` branch; the case hands the number over as an issue reference
in the prompt and scaffolds its sandbox on `main`, which is `/tdd #190` typed off-branch — the one
path the hook cannot reach. The case survives untouched, and the grader carries a comment saying so,
because the next reader will have the same first thought.

Ask this of any case you write: if the behavior could be made impossible, should it be an eval at
all? The residue is the useful part — what survives from `plans-with-sliced-reading` is the half a
guard cannot judge, that the *right* sections were chosen, and it is worth a case only if it can be
graded on something better than which tool fired. `resumes-from-plan` keeps two such halves outright:
whether the recorded shas are reconciled against `git log`, and whether the resume point is stated
back for confirmation. A hook can hand a plan over; it cannot make one be doubted.

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
- Copy a tool the case needs into the fixture rather than reaching out of the sandbox for it. It
  drifts, and a stale copy is the cost of a hermetic sandbox. No case carries one today — the one
  that did (a `doc-slice` copy) became a hook. `tests/test_claude_hooks.py` shows the alternative
  where the harness allows it: copy the real tool in at run time, so it cannot go stale at all.
- A fixture is a directory, **not a repository** — a nested `.git` is not committable. A case whose
  behavior needs git history (`tdd/resumes-from-plan` reconciles a plan against `git log`) supplies
  it with `context.scaffold_script`, and must state what the case degrades to when the run is not
  given `--scaffold`, since that flag is off by default.
- A fixture that asserts something about *size* states its own measurements in its header
  (`tdd/resumes-from-plan/fixture/docs/module-maps/leverage-surface.md`). A comment claiming a file
  is "large" is unfalsifiable and goes stale the first time someone edits it. State one unit and the
  range you actually mean: that map's h3 module sections run 275–2,356 characters, while its h2
  wrappers run from 165 up to 15,204, so "a section runs …" without the qualifier is false.
- A fixture standing in for a `doc-slice` target must not carry a **section name inside its title**.
  `doc-slice` matches a heading substring and falls back to the first match, so a map titled
  "Leverage surface" answers `doc-slice … Leverage` with the whole file — through the very tool the
  case is checking the agent reached for, past a `Read`/`cat` grader pair that sees nothing wrong.
  Name the file after its subject and the title after something broader; the map above is titled
  "Perps margin surface" for exactly this reason, and says so in its own header.

### Grading a read, and grading its absence

A `max: 0` grader keyed on the `Read` tool does not prove a file went unread: `cat` loads the same
bytes through `Bash` and scores zero on it. Pair every `max: 0` on `Read` with the matching
`tool_used: Bash, input_match: cat <path>`. The evasion is not hypothetical, since the
bypass-permissions guidance actively pushes reads toward `cat` — which is why
`no-unsliced-doc-reads` binds both tools, and why the case that used to demonstrate this pairing
here is the one that became that hook.

For the mirror-image claim — that a file *was* consulted — grade the **trace**, not the tool:
`regex` with `target: trace` over the path catches the `Read` and the `cat` alike
(`tdd/resumes-from-plan`'s `plan-consulted`). Keying that one on `Read` would fail a run that
complied through `Bash`.

Prefer a pair of `tool_used` graders to a `tool_order` whenever the `after` side is optional. A
compliant run often stops before reaching it, and a `tool_order` whose `after` never fires has no
defined verdict.

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
