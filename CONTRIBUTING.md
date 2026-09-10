# Contributing to Tickwright

Thanks for your interest in Tickwright — a readable, event-driven algorithmic trading engine
built as a reference implementation. **Clarity and correctness are the priorities**; please keep
changes small, well-tested, and easy to read.

## Before you start

- Read [`CONTEXT.md`](CONTEXT.md) — the project's vocabulary, every term resolved.
- Skim the relevant [`docs/adr/`](docs/adr/) — the locked-in architectural decisions and *why*.

## Development setup

- **Python 3.13**, managed with [uv](https://github.com/astral-sh/uv).
- `uv venv` → `uv sync` → `source .venv/bin/activate` (or prefix commands with `uv run`).
- Dependencies are always project-local — **never installed globally**.
- **Enable the git hooks** once per clone: `git config core.hooksPath .githooks`. This is shared
  config, so linked worktrees inherit it — no need to re-run it in each one.

## Running checks

| Check | Command |
| ----- | ------- |
| Tests | `uv run pytest -v` (property tests via `hypothesis`; target ≥90% coverage on the core) |
| Lint  | `uv run ruff check .` |
| Format | `uv run ruff format .` |
| Types | `uv run mypy` (no `.` — the argument overrides `files` in `pyproject.toml` and skips the hidden `.claude/hooks`) |
| Imports | `uv run lint-imports` (dependency-direction boundaries, ADR-0032) |

The default paper-exchange + in-memory-bus path runs with **no external services and no API keys**.

### Test tiers

`uv run pytest` on a bare clone is **hermetic** — paper exchange + in-memory bus + SQLite, plus
`hypothesis` property tests — and needs no services or keys. The `KafkaBus` adapter is covered here
too: its unit tests run against an in-process fake broker
([`tests/_support/kafka_fakes.py`](tests/_support/kafka_fakes.py)), not a real cluster.

Two markers reach for real infrastructure and **auto-skip** when it isn't configured, so the default
run stays green without them. They differ in CI: `postgres` is hermetic (a pinned container, offline,
deterministic) and **is** in the gate; `live` reaches a real venue and never can be.

- **`postgres`** — the `PostgresStore` contract
  ([ADR-0019](docs/adr/0019-durable-store-sqlite-default-postgres-second.md)). Auto-skips unless
  `STORE_POSTGRES_DSN` points at a reachable server. Bring one up with `docker compose up -d postgres`,
  then run `STORE_POSTGRES_DSN=postgresql://tickwright:tickwright@localhost:5432/tickwright uv run
  pytest -m postgres`. `-m "not postgres"` deselects them. CI runs this tier against a `postgres:17`
  service on the `ci` job (issue #253) — locally it stays opt-in, so `uv sync && uv run pytest` needs
  no Docker.
- **`live`** — places **real orders on Hyperliquid testnet**
  ([ADR-0022](docs/adr/0022-testing-strategy.md)). Auto-skips unless you opt in with
  `TICKWRIGHT_LIVE_TESTNET=1` **and** `TICKWRIGHT_HYPERLIQUID__SIGNING_KEY` holds a funded testnet key
  — the key alone never enrolls the suite, so CI can run everything under a hostile config. Run it
  locally with `TICKWRIGHT_LIVE_TESTNET=1 uv run pytest -m live`. In CI it runs from
  [`ci-live.yml`](.github/workflows/ci-live.yml) — weekly plus `workflow_dispatch` — and is **never**
  part of the merge gate: it reaches a system we do not control, and on a public repo its secrets are
  unavailable to fork PRs, so as a gate it would silently degrade for contributors.

To exercise the real `KafkaBus` end-to-end (rather than its fake-broker unit tests), run the *app* on
Kafka, not the suite: `docker compose up -d kafka`, then `TICKWRIGHT_BUS=kafka uv run tickwright`.
[`.env.example`](.env.example) is the canonical reference for every backend's variables.

### Skill evals (agent tooling, not the engine)

One more tier sits outside `pytest` entirely: [`evals/`](evals/README.md) tests the skills in
`.claude/skills/` — that `/tdd` still enforces red-before-green, that `/unslop` still cuts the tells
— because nothing in `pytest` reads them and a skill can stop working with no failing check.
Cases run with `claude plugin eval .` from the repo root, each in a with-skill and a without-skill
arm so the delta shows whether the skill is doing anything. It is **not** a merge gate: scores are
noisy and the runner reaches the API. Run it when you edit a skill. You only need this if you are
changing agent tooling; contributing engine code does not.

### Git hooks (local convenience, not the gate)

With `core.hooksPath` enabled (see setup):

- **pre-commit** auto-runs `ruff check --fix` + `ruff format` on your *staged* Python and re-stages
  the result (unstaged hunks are preserved safely).
- **commit-msg** runs your local hook guard if you have one (see below), and does nothing otherwise.
- **pre-push** runs `mypy` on changed Python before the push leaves your machine.

They exist to catch problems early; the **authoritative gate is CI**, which re-runs everything and
can't be skipped with `--no-verify`.

### Agent-loop hooks (Claude Code)

Git hooks fire at commit time. An agent breaks a rule *mid-loop*, dozens of tool calls earlier, and
by the time a commit exists the cost is already paid. `.claude/hooks/` closes that window: seven
scripts wired in the committed `.claude/settings.json`, each reading the event on stdin. They come
in two shapes, and the difference is not cosmetic.

**Five guards refuse.** They run on `PreToolUse` and exit `0` to allow or `2` to block, with the
reason on stderr going back to the agent.

| Guard | Binds | Refuses | Stays allowed |
| ----- | ----- | ------- | ------------- |
| `no-tracked-writes` | `Bash` | `sed -i`, a redirect in any shape that names a file (`>`, `>>`, `>\|`, `&>`, `&>>`, `>& file`) or `tee` aimed at a **git-tracked** file, or at a **glob** that matches one | creating a new file; `2>&1` and `>&2`, which name a descriptor; `tee` as a grep **pattern**; a heredoc **body** that quotes a write; a **directory**, whose contents are not the target; anything outside the repo |
| `no-excluded-reads` | `Bash` | `cat`/`head`/`grep`/… of a path `git check-ignore` matches | `.agents/plans/`; a program in the **executable position**, so `.venv/bin/ruff` still runs; a grep **pattern** that merely spells an ignored path; a redirect **target**, so a run still reports into `logs/` |
| `no-global-installs` | `Bash` | `pip install`, `uv pip install --system`, `uv tool install`, `pipx`, `brew`, `npm -g` — and the same behind a `sudo` or inside a loop body | `uv add`/`uv sync`/`uvx`/`uv tool run`; `uv pip install` without `--system` |
| `no-unsliced-doc-reads` | `Read`, `Bash` | a whole read of `CONTEXT.md`, an ADR, a module map or a research note — by `Read`, or by `cat`/`less`/`nl`/…, and through a **glob** that expands onto the corpus | `head`/`tail`/`sed -n`/`grep`, which are already the slice; `doc-slice`; a `Read` with an explicit `offset`/`limit`; a redirect **target**, which is a write |
| `no-unlinked-prs` | `Bash` | a `gh pr create` whose `--body`/`--body-file` carries no `Closes #N` | every body it cannot see — `--fill`, `--web`, an editor, `-F -` |

**Two answer instead**, because there is nothing to refuse. They run on events with no veto, which
is the right shape rather than a limitation worked around.

| Hook | Event | Does |
| ---- | ----- | ---- |
| `ruff-on-write` | `PostToolUse` on `Edit`, `Write` | runs `ruff format` and `ruff check --fix` on the one `*.py` file just written, then reports the rewrite and anything it could not fix |
| `resume-from-plan` | `SessionStart` | prints the current slice's open behaviors, read off `ralph/issue-<N>` and `.agents/plans/issue-<N>.md` |

Two of the **guards** answer rather than merely refusing — `no-unsliced-doc-reads` and
`no-unlinked-prs`, back in the table above — and that is the property worth copying.
`no-unsliced-doc-reads` returns the file's own index — the `doc-slice` table of contents, with each
section marked `(+N)` for the amendment blocks it carries, or for `CONTEXT.md` its terms and their
line numbers. `no-unlinked-prs` returns the `Closes #<N>` line the branch implies. In both cases the
rule they replace lost on economics rather than on clarity: complying cost an extra call and
ignoring it cost nothing, so the wrong path was the cheap one. Answering makes them equal.

The two fast-feedback hooks front-run checks that already exist and replace neither. `ci` runs
`ruff format --check .` and `ruff check .` and **reports only** — so a formatting slip used to cost
a red run, a fix commit and a second run, for a change the binary in `.venv` makes in milliseconds.
`pr-policy`'s *Body closes an issue* step is likewise correct and likewise late. Moving the answer
earlier is the whole of it; the floor does not move.

Four properties are deliberate:

- **Committed, not local.** The point of turning a rule into a hook is that it reaches *other
  people's* agents. One in `settings.local.json` would be the tribal knowledge it replaced.
- **Derived, not listed.** Both path guards ask `git`; both fast-feedback hooks read the issue
  number off the branch. Copying `.gitignore`'s globs into a hook would be two lists that must agree
  — the drift this project calls a bug — and the derived form also covers whatever gets ignored
  next. The same rule applies inside `.claude/hooks/`: the redirect shapes live once in `_shell.py`,
  because what the write guard collects is exactly what the two read guards discard, and a file
  being written to is not one being read. Two exceptions exist because there is no predicate to
  derive from: `no-unsliced-doc-reads`'s corpus, since "long enough to be worth slicing" is
  editorial, and `no-unlinked-prs`'s pattern, which must match `pr-policy`'s exactly. Each is held
  by a test instead — that every glob still names a real file, and that the pattern still appears
  verbatim in the workflow — so a rename fails loudly rather than disarming a hook in silence.
- **They fail open.** A command the lexer cannot parse is allowed through, a body that is not
  visible is not judged, and a missing `.venv/bin/ruff` is silence rather than a complaint. A hook
  that misfires on input it does not understand is one an agent learns to route around, which costs
  more than the call it wrongly blocked. That licence covers a *parse*, never a token the guard read
  in the wrong position — a wrapper (`sudo pip install`), a reserved word standing in front of the
  program (`do`, `then`, `time`), a redirect written before it (`2>/dev/null pip install`), a grep
  pattern, a `tee` being searched for rather than run, or a heredoc body all lex perfectly, so
  `_shell.py` and the executable-position test resolve each one rather than shrugging at it. The
  redirect is peeled **only** at the head, which is what makes it safe: a quoted `'>'` is
  indistinguishable from the operator, but a pattern is an argument, and nothing standing before the
  program is data. A leading **input** redirect (`< CONTEXT.md cat`) is the one shape left unpeeled
  and so the one read of this kind that still goes through: its operand *is* a file being read, so
  dropping it would lose a path while keeping it leaves that path standing where the program does.
  It needs an answer of its own rather than this one. The inverse holds too: a shape the lexer *does*
  produce is not a shape the guard may miss, which is why the redirect set enumerates `&>` and `>|`
  instead of the two spellings that come to mind first, why `src/*.py` is handed to `git` to resolve
  rather than judged by how many files came back — counting them would allow a write in proportion
  to how many it rewrites — and why a `~` is expanded before a pattern is matched rather than only
  after, since `glob` leaves a user prefix alone and would let `~/repo/docs/adr/*.md` match nothing.
  What stays out of reach is a *value*: `sed -i '' s/a/b/ $f` lexes cleanly and stands in the right
  position, and no lexer knows which file `$f` names. Three shapes sit beside that one and are out
  of **scope** rather than out of reach, each decidable and none decided: `$HOME/repo/CONTEXT.md`,
  whose value a guard already reads to expand the `~` spelling of the same path; brace expansion
  (`docs/{adr,module-maps}/*.md`), which nothing here expands; and a `cd` in an earlier segment,
  which would mean tracking a working directory across a command rather than reading one off the
  event. Each is a whole-file read that goes through, so they are listed here rather than left to
  be rediscovered one at a time.
- **`.venv/bin/<tool>`, never `uv run <tool>`.** `uv run` re-syncs the environment before handing
  over: 1.99 s against 0.013 s for the binary. A two-second tax on every edit is a hook someone
  turns off, and a hook that is off enforces nothing.

`ruff-on-write` deliberately does **not** run mypy. Ruff's half *fixes*, so it spends no context and
asks for no judgement; a type check can only report, and mid-red a TDD step legitimately
type-errors. The note would be noise on exactly the edits the hook fires hardest on, and an agent
that learns to skim it skims the "this file was rewritten, your copy is stale" line beside it. `ci`
keeps mypy, where a complete diff makes the reading trustworthy.

Same standing as the git hooks: **local convenience, not the gate.** They are Claude Code-specific,
so a contributor using another tool — or none — gets nothing from them, and CI stays the floor for
everyone. They are ordinary stdlib Python with no network access; read them before you trust them.
`tests/test_claude_hooks.py` fences all seven, and asserts the wiring too — across every event key,
since a hook filed under the wrong event passes every direct-invocation test while never being
called. Hook config is read when a session starts, so restart Claude Code after changing one.

#### Local hook guards

`pre-commit` and `commit-msg` each finish by handing off to an optional **local hook guard**: an
executable you keep outside the repo, so it is never published and runs only on the machines that
have it. Nothing here installs one, and you do not need one to contribute — but if you add one,
this is the contract it is held to:

- **Location** — `hooks-local/<hook-name>` under the *common* git directory, so
  `.git/hooks-local/pre-commit` and `.git/hooks-local/commit-msg`. The hooks resolve it with
  `git rev-parse --git-common-dir` rather than `--git-dir`, which is what lets linked worktrees
  share the one install instead of quietly skipping it.
- **Arming** — the executable bit. A guard that is absent or non-executable is skipped and the
  commit proceeds. That is the normal case for nearly every clone, so it is silent by design.
- **Authority** — a guard that exits non-zero aborts the commit, and that veto is final. Any change
  to the delegation must keep a failing guard failing: this is the seam the maintainer's private
  reference scrub hangs off, backing the "no code is copied from any prior/private codebase" rule
  in [`CLAUDE.md`](CLAUDE.md). `tests/test_githooks.py` fences both halves against a real `git`.

#### Per-worktree vs shared git state

Linked worktrees share one **common** git directory — `refs/stash`, `hooks-local/`, and config all
live there and are seen identically from every worktree — but each worktree also has its **own**
private git dir (`.git/worktrees/<name>`). A hook that reads or writes git state must pick between
the two deliberately, and every hook comment that names a `--git-dir` / `--git-common-dir` /
`refs/worktree/` choice is applying this one rule:

- **Shared** state must be resolved so all worktrees agree on it. The local hook guard is looked up
  with `git rev-parse --git-common-dir` (not `--git-dir`, which is `.git/worktrees/<name>` in a
  linked worktree, where the guard would be invisible), so one install serves every worktree.
- **Per-worktree scratch** must never land on shared state, or two concurrent hooks collide and
  silently swap each other's uncommitted work. `pre-commit`'s unstaged-patch snapshot goes under
  `--git-dir` (the private dir); `pre-push`'s autostash is parked under `refs/worktree/` (git's
  per-worktree ref namespace) and stays off the shared `refs/stash` stack entirely, so a stash the
  hook created is the only thing it can restore. `tests/test_githooks.py` fences the `pre-push` case
  against a real linked worktree.

This matters most under parallel worktrees, where concurrent hooks are the norm rather than the
exception.

## How we work

- **Spec first.** Requirements are stress-tested ("grilled") into a written specification before
  any code. Load-bearing decisions are recorded as **ADRs** under [`docs/adr/`](docs/adr/), and the
  shared domain language lives in [`CONTEXT.md`](CONTEXT.md). Read these before proposing a change.
- **Vertical slices.** A change crosses every relevant layer (feed → strategy → exchange → engine)
  in a single PR, rather than delivering one horizontal layer in isolation.
- **Test-driven.** Write a failing test first, make it pass, then refactor — one behavior at a
  time. Mock only at process boundaries (HTTP/WS, Kafka, the clock, randomness); never mock the
  engine's own classes.
- **Small commits.** One logical change per commit. Match the surrounding naming and style.
- **Docs stay in sync.** If a change alters something documented elsewhere — workflow conventions
  (`docs/agents/`, `docs/workflow/`), agent skills (`.claude/skills/`), ADRs, `CONTEXT.md`, CI
  workflows — update every affected file in the same PR. Prefer one canonical source that others
  link over copies that can drift.

## Conventions

- **Branches:** `<type>/<short-slug>` — e.g. `feat/per-symbol-ordering`, `fix/ghost-reconcile-race`.
- **Commits:** [Conventional Commits](https://www.conventionalcommits.org/) — `type: imperative subject`
  (`feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `ci`). One logical change per commit.

## Pull requests

- **One PR per change — no mixed-concern PRs.** Reference the issue it closes in the body
  (`Closes #N`); don't close issues by hand — merging the PR closes them.
- Keep the diff focused and the description clear about *what* changed and *why*.
- A PR is mergeable once it passes review and the lint / type / test / coverage gate.

## Code style

- Line length **100**; Ruff formatter (double quotes); Ruff lint rules `E, W, F, I, B, C4, UP`;
  mypy with `check_untyped_defs`.
- Prefer **deep modules with small interfaces**, clear names, and comments only where they earn
  their place.

## License

By contributing, you agree that your contributions are licensed under the project's
[Apache-2.0](LICENSE) license.
