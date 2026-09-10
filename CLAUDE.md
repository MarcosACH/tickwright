## What Tickwright is

Tickwright is a trading engine. A user brings config and a strategy. The engine handles the rest:
market feed, event flow, order execution, crash recovery, and exchange reconciliation. It is
designed to run against testnet and against real money.

It is built to be extended. v1 covers Hyperliquid market data, an in-process deterministic paper
exchange, and the `InMemory` and `Kafka` bus backends. More venues come later, and the architecture
exists to make that cheap. We are building the foundation right now, so foundation quality matters
more than feature count.

Full scope and non-goals: `README.md`.

## Never compromise these

A change that damages one of these is wrong, even when it is good in every other way.

1. **Safety.** Real money can move through this engine. A bug here costs a user real funds. When
   correctness and speed conflict, pick correctness. When you are not sure a path is safe, stop and
   ask me.
2. **Recovery.** The order saga has to survive a crash. Consumers stay idempotent. Events stay
   replayable. Reconciliation stays able to tell the truth after a restart.
3. **A hermetic default path.** The paper exchange plus in-memory bus must run with no external
   service, no API key, and no ambient config. See Configuration below.
4. **The seams.** A seam exists so a second venue or backend can plug in. Do not collapse one
   because a single implementation would be shorter today.

Review-blocking detail lives in `docs/agents/invariants.md`.

## A note from Marcos

I am not a native English speaker. I read every message you write. When your writing is dense I
lose time and I lose the thread. Plain writing is not a style preference here. It is how I stay in
control of my own project.

I like simple systems. Do not keep complexity just because it is already there. Do not add
machinery because it looks impressive. Find the real constraint, then build the smallest thing that
makes the correct behavior obvious.

Ask me when you are unsure. A question costs one message. A wrong assumption costs a session.

## How to write

This is the rule I care about most.

**Length**

- Default to under 150 words.
- Past that, use a list or a table. Never a wall of prose.
- Past 400 words, ask me first.

**Sentences**

- One idea per sentence. Aim for 15 words. Never go past 25.
- Do not use em dashes or semicolons. If a sentence seems to need one, it is two sentences.
- Use the common word. Say "use" not "leverage". Say "is" not "serves as".
- No aphorisms and no clever inversions. Say the plain thing.
- Sentence case headings. Straight quotes. No decorative emoji.
- No filler openers like "Of course!" or "Great question!".

This applies to chat, PR titles and bodies, issue text, commit messages, code comments, and
docstrings. It applies to any new line in any doc. Do not rewrite old text to match it.

**PR and issue titles.** Say why the change matters, in words I could read out loud.

- Bad: `fix(engine): one absent read never ghosts a resting order, at boot no less than in flight`
- Good: `fix(engine): keep resting orders when the exchange read fails at boot`

**PR and issue bodies.** Open with the problem, in the words I used when I asked for it. Then the
solution. Never open with a list of what you implemented.

- Bad: "The `MarketFeed` seam was two members and one line of prose, and since #175 both adapters
  owed a second thing that line never mentioned."
- Good: "A feed could pass the type checker without ever publishing a mark. When that happened,
  every P&L number silently read `None`, and no test went red."

**Code comments.** Say why, not what. Update them when the code around them moves.

## Rules and defaults

A **rule** is not negotiable. If my prompt conflicts with one, stop and tell me before you act.
Everything else here is a **default**. My prompt wins, and you do not have to argue for it.

Rules:

- **TDD.** Red test, then green implementation, then refactor. One behavior at a time.
- **Vertical slice.** A change crosses every layer it touches (feed, strategy, exchange, engine) in
  one PR. Never ship a single layer alone.
- **One PR per issue**, targeting `main`, with `Closes #N` in the body. Never close an issue by
  hand. The merge does it. Two things have no merge event of their own: a parent PRD closes at
  release, and a wayfinder ticket closes on resolution (ADR-0050).
- **Releases.** At a shippable milestone, propose a version and its SemVer reason, then wait for my
  sign-off. Never tag or publish a release yourself. See `docs/workflow/versioning.md`.
- **Docs-sync.** If a change makes another file wrong, fix that file in the same PR. Link one
  canonical source instead of copying it. When two files disagree, fix the copy, not the canon.
- **Dependencies.** Use `uv` against the project `.venv`. Never install globally.
- **No copied code.** Never copy from a prior or private codebase. Build from first principles, then
  check the result against current practice.

Defaults:

- Prefer small, targeted edits over rewrites. Match the naming and style around you.
- Two implementations per seam, no more. One is hard to tell apart from hardcoding. Three is scope
  creep.
- One logical change per commit, not one file per commit.

## Workflow

Single repo, vertical-slice issues. Skills live in `.claude/skills/` and describe their own
triggers.

1. `/grill-with-docs` sets requirements and scope. No PRD and no code until I sign off.
2. `/to-spec` writes the parent PRD issue. Synthesis only, with no second interview.
3. `/module-map` writes the architecture anchor at `docs/module-maps/<slug>.md`.
4. `/to-tickets` splits the PRD into vertical-slice child issues, each declaring what blocks it. The
   last child is always `integrate-and-verify`, blocked by all its siblings (ADR-0050).
5. `/tdd` runs red-green-refactor on `ralph/issue-<N>`. The confirmed plan goes in
   `.agents/plans/issue-<N>.md`. The `resume-from-plan` hook reprints its open behaviors at session
   start. Recorded shas are hand-written, so check them against `git log`.
6. One PR per issue, with `Closes #<N>` in the body.
7. `/code-review` reports BLOCKING, WARN, and NIT. Label `ralph:ready` when it is clean.

Conventions: `docs/workflow/labels.md`, `docs/agents/issue-tracker.md`.

## Context discipline

- **Read `CONTEXT.md`, ADRs, module maps, and research notes by section, never whole.** Use
  `.agents/tools/doc-slice <file> <heading-substr>`, or `--amendments <file> [<heading>]` for
  corrections. **ADRs are append-corrected.** A section's `**( … **)**` blocks hold the current
  truth. The prose above them is often the retired version, so reading in document order gives you
  the superseded decision. Canon: `docs/agents/adr-reading.md`.
- Never read ignored paths such as `.venv/`, caches, `logs/`, or `*.pyc`. Membership is
  `git check-ignore`, so `.gitignore` is the one list. `.agents/plans/` is ignored and readable by
  design.
- For large source and test files, use `Read` with `offset` and `limit` on the symbol you need.
- Read GitHub issues with `gh issue view <N>`.
- **Edit a tracked file with `Edit`, not a Bash write.** A Bash write stales the harness's cached
  copy and costs a full re-read.
- Hooks in `.claude/hooks/` enforce the rules above at the call, and they answer with the right
  invocation instead of a bare refusal. They are why none of this has to be remembered. See
  [`CONTRIBUTING.md` → Agent-loop hooks](CONTRIBUTING.md#agent-loop-hooks-claude-code).

## Setup and checks

```bash
uv venv && uv sync    # one-time
uv run pytest -v      # tests
uv run ruff check .   # lint
uv run ruff format .  # format
uv run mypy           # types (bare: `mypy .` overrides files= and skips .claude/hooks)
uv run lint-imports   # dependency-direction boundaries (ADR-0032)
```

The default paper-exchange and in-memory-bus path needs no external service and no API key. Test
tiers, the `postgres` and `live` markers, skill evals, and the Docker services are all documented in
[`CONTRIBUTING.md` → Running checks](CONTRIBUTING.md#running-checks). Read it before you reach for
one.

Two things about tests are easy to get wrong, so they are here too:

- **Mock at process boundaries only** (HTTP, WebSocket, the Kafka client, the system clock,
  randomness). Never mock our own classes.
- **Stay hermetic against ambient config.** An outcome must never depend on a developer `.env` or an
  exported `TICKWRIGHT_*` variable. Build the pure `AppConfig`, never `AppSettings`. Hand any
  subprocess an environment with `TICKWRIGHT_` scrubbed.

A `.py` file you just wrote through `Edit` or `Write` is already fixed and formatted. The
`ruff-on-write` hook does it at the call and reports what it could not fix. It rewrites the file, so
re-read before your next edit. It does not run mypy, because a red TDD step is allowed to
type-error. Style is 100-char lines, the Ruff formatter, and rules E, W, F, I, B, C4, UP.

## Configuration

**`.env.example` is the canonical variable reference.** It lists every variable, its default, its
constraints, and the ADR behind it. Each one maps onto a field of `AppConfig`
(`src/tickwright/app/config.py`) using the `TICKWRIGHT_` prefix, `__` for nesting, and JSON for
complex values. The behavior behind a variable belongs to its ADR. Start at `.env.example` and
follow the citation.

`config.py` holds two classes and the split is load-bearing (issue #71). `AppConfig` is a pure
`BaseModel` that reads nothing ambient. It is what `build_engine` takes and what tests build.
`AppSettings` adds the env and `.env` skin on top, and `__main__.py` is its only legitimate builder.
Reading ambient config anywhere else, including in a test, lets a developer `.env` or an exported
variable outrank the class defaults. That can silently wire a live venue into a paper path.
`AppSettings` stays out of `app`'s `__all__`. Do not export it.

## Reference

- Domain glossary: `CONTEXT.md`
- Architecture decisions: `docs/adr/`, read by section, append-corrected
- Module maps for in-flight features: `docs/module-maps/`
- Research notes: `docs/research/`, dated captures that are **not maintained**. Where a note and an
  ADR disagree, the ADR wins.
- Review-blocking invariants: `docs/agents/invariants.md`
- Labels and triage: `docs/workflow/labels.md`, `docs/agents/triage-labels.md`
