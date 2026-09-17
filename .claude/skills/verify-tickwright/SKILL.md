---
name: verify-tickwright
description: "Use when asked to verify, prove, or smoke-test the real tickwright engine (the CLI process) the way an operator runs it: paper replay, crash recovery, kill switch, reconciliation, fees and funding, Kafka, Postgres, the Hyperliquid feed, and the Hyperliquid testnet exchange. Drives real processes and keeps evidence. Not for unit tests, use `uv run pytest` for those."
---

# Verify tickwright

Tickwright is a non-interactive CLI. You configure it with a `.env`, start it, and it runs until
you send a signal. There is no prompt and no UI. Verification means: start a real process in a
scratch workspace, make it trade on a known tick stream, send it operator signals, and read what
it wrote to its log and its store.

Everything goes through one helper. Call it `V` below.

```bash
V=.claude/skills/verify-tickwright/scripts/verify
```

Read `features/README.md` before driving. It is the map of what to prove.

## Facts you need before driving

- Logs are JSON lines on **stderr**, one `"event": "<name>"` per line. The event names are the
  catalog in `src/tickwright/observability/catalog.py`.
- State is the store: SQLite file by default, Postgres when selected. Tables: `orders`,
  `positions`, `account`, `kill_switch`, `funding_marks`, `strategy_snapshots`. Order states are
  stored lowercase (`filled`, `live`, `rejected`, `denied`).
- Under the replay feed, time is virtual. The clock jumps to each tick's `ts_event`. So a tick
  file with rows 90 seconds apart runs the 90 second ghost grace, and a row past the top of a UTC
  hour settles paper funding. When the file ends, time stops. Nothing paced by the clock fires
  again until the process is stopped.
- Under the Hyperliquid feed, time is the wall clock and the cadences run for real.
- Signals: `TERM` or `INT` stop gracefully (exit 0). `KILL` is a crash (exit -9, no final
  snapshot, but the last per-callback snapshot is on disk).
  `USR1` trips the durable kill switch, `USR2` resets it. A config refusal or a fault exits 1.
- Two instances never share a store, a Kafka topic, or a scratch dir. The helper gives each run
  its own. Never drive `tickwright.db` or `.env` at the repo root. Those are the operator's.

## Launch

There is no long-lived server. Each drive is one or more short-lived **lives** of the process in
one **run**. One run covers one feature file.

```bash
$V init <run-id> --feature <feature>              # .agents/verify/<run-id>/{scratch,evidence}
$V ticks <run-id> ticks --row BTC:42000@0 --row BTC:42100@1s   # scratch/ticks.jsonl
$V start <run-id> first --preset paper-replay --env 'TICKWRIGHT_STRATEGIES=[...]'
$V await <run-id> first --event engine.feed_started
```

`start` writes `scratch/.env` from the preset plus your `--env` overrides, spawns
`python -m tickwright.app` in `scratch/` with every `TICKWRIGHT_*` variable scrubbed from the
inherited environment, and returns at once. A life is ready when `engine.feed_started` is in its
log. Presets:

| Preset | What it wires | Needs |
| --- | --- | --- |
| `paper-replay` | replay `ticks.jsonl`, paper venue, SQLite, in-memory bus, BTC and ETH specs, genesis 100000, no strategies | nothing |
| `paper-livefeed` | Hyperliquid **mainnet** public trades into the paper venue | network, no key |
| `testnet` | Hyperliquid **testnet** feed and exchange | `--forward-key` and a funded key in the repo `.env` |

`--strategy <name>` runs `scripts/run_strategy.py` instead of the CLI, with one of the four
verification strategies (`round_trip_then_flat`, `take_profit`, `margin_watch`, `dual_symbol`).
Their knobs go in as `--param KEY=VALUE`. See `features/portfolio-strategies.md`.

Kafka and Postgres need Docker. `$V infra <run-id> up kafka` or `up postgres` starts the compose
service and records that this run owns it. A service you already had running is used and never
owned, so `cleanup` leaves it up. Postgres gets a database per run and the helper prints the DSN
to pass as `TICKWRIGHT_POSTGRES__DSN`.

Teardown is `$V cleanup <run-id>`. See Cleanup.

## Doctor

```bash
$V doctor
```

Read-only. Prints the branch and sha, whether the tree is dirty, whether `tickwright` imports
from the venv, whether Docker is up, whether the repo `.env` holds a signing key, and which
verification instances are still running. Run it first, and again after anything surprising. If
the package is not importable, run `uv sync` and stop until it is.

## Drive

One life, from start to proof:

```bash
$V start <run> <life> --preset paper-replay --env KEY=VALUE ...   # or --strategy <name> --param K=V
$V await <run> <life> --event order.filled                # or --event X --count 2
$V await <run> <life> --order $($V cloid shooter:BTC:1)=FILLED
$V await <run> <life> --sql "select cash from account" --expect 100081.05500000
$V signal <run> <life> TERM                               # TERM INT KILL USR1 USR2
$V await <run> <life> --exit                              # prints the exit code
$V dump  <run> <life>                                     # store tables + event counts
$V check <run> <life> <check-id> --sql "..." --expect X   # records PASS or FAIL
$V check <run> <life> <check-id> --file first.offsets.txt --expect X   # an evidence file's content
$V report <run>                                           # writes and prints REPORT.md
```

- `cloid` turns a signal id (`<strategy_id>:<symbol>:<seq>`) into the order's cloid, so you can
  wait on the store row for a specific order.
- `await` polls up to `--timeout` (default 30 s) and prints the matching log line or row. On
  timeout it prints the last five log lines and exits 1. `--sql` needs `--expect`, and a query
  with no rows never matches.
- A second life on the same run reuses `scratch/store.db`. That is how you verify a restart.
  Delete the file between lives when you want a fresh store.
- Two tick files in one run: `$V ticks <run> second --base 2024-01-01T00:00:05+00:00 ...` and
  `--env TICKWRIGHT_REPLAY__PATH=second.jsonl`.

Every recipe in `features/` uses only these verbs. `await` is how you wait. `check` is how you
judge. Do not skip a `check` because the `await` already matched: the await proves timing, the
check proves the value and writes it down.

## Evidence

Everything a life produced lands in `.agents/verify/<run-id>/evidence/`:

| File | What it is |
| --- | --- |
| `run.json` | run id, sha, branch, start time, owned infra |
| `<life>.env.txt` | the exact `.env` the life ran with, plus each `--param` as a `# VERIFY_` comment |
| `<life>.stderr.jsonl` | the engine's JSON log, complete |
| `<life>.stdout.txt` | the engine's stdout, normally empty |
| `<life>.exit` | the exit code |
| `<life>.signals.txt` | every start, signal and exit, timestamped |
| `<life>.store.txt` | `dump`: the store tables |
| `<life>.events.txt` | `dump`: event counts and the order, position, account, engine trail |
| `<name>.jsonl` | a copy of each tick file used |
| `checks.txt` | one `PASS` or `FAIL` line per `check`, with `expected=` and `got=` |
| `REPORT.md` | `report`: the verdict, the FAIL lines, alarm events, exit codes, store tables |
| `ISSUE.md` | `issue-draft`: a bug body for the FAIL lines, when there are any |

Proof standards:

- Drive the real path. Ticks in through the feed, signals in through the bus, orders out through
  the venue. Never write the store by hand and never call an engine method to set state.
- Capture the action and the state it caused. A `signal` line plus the event it produced plus the
  store row it changed. Run `dump` at the end of every life.
- Check the side effect in the store, not only the log. A fill is proven by the `orders` row and
  the `positions` row, not by `order.filled` alone.
- Say which number you expected and show the one you got. The feature files give the arithmetic.
- Mocks: none. The paper venue is a real implementation of the seam, not a mock. The mainnet feed
  and the testnet exchange are the real venue.
- The testnet key never enters the evidence. `cleanup` and `secrets-check` scan for it.

The evidence directory is gitignored and readable. Point the user at it in your report.

## Report

A run is read from one file. After the last `check`:

```bash
$V report <run-id>          # exit 0 on PASS, 1 on FAIL or NO CHECKS
$V runs                     # every run: id, start, feature, sha, verdict
```

`REPORT.md` opens with `# <run-id>: <feature> PASS|FAIL (n of m checks failed)`, then the FAIL
lines, then every alarm event found in the logs (`engine.faulted`, `order.rejected`,
`order.denied`, `ghost.reconciled`, `account.healed`, `valuation.divergence`, and the rest of
`ALARM_EVENTS` in `verify.py`), then exit codes, all checks, and the store tables.

Reading it:

- **FAIL lines are the verdict.** Each names its check id from the feature file's
  `Sub-features`, the life, and both numbers.
- **Alarm events are a prompt, not a verdict.** `ghost.reconciled` is the ghost feature working.
  `engine.faulted` in `config-refusals` is the refusal working. An alarm with no check that
  expects it is worth a look.
- **Expected FAILs exist.** A feature file's Gotchas may name a known FAIL with a date. If the
  FAIL line matches, it is already known. Say so and move on. If a FAIL is not named there, it is
  new.
- `NO CHECKS` means the recipe was not finished. It is not a pass.

Comparing two runs of one feature, before and after a fix, is `$V runs` plus the two
`REPORT.md` files. `$V prune --keep 5` drops the oldest runs per feature when the tree grows.

## Filing

A new FAIL becomes a bug the user files. The skill drafts, the user files.

```bash
$V issue-draft <run-id>     # writes evidence/ISSUE.md and prints it; exit 1 when nothing failed
```

The draft follows `.github/ISSUE_TEMPLATE/bug_report.md` and links the feature file, the
commit, `REPORT.md`, and the failing life's log and store dump. Before handing it over:

1. Say what the number means in one plain sentence under "What happens".
2. Name the ADR or CONTEXT.md term the expected value comes from under "What should happen".
   `/why` on the failing path finds it when you do not know.
3. Hand the user the draft path and the `gh issue create` line at the bottom of it. The rest of
   the filing rules (labels, assignee, project, Status) are in `docs/agents/issue-tracker.md`.

Never run `gh issue create` yourself from this skill. Never edit engine code from this skill. The
fix is a `/tdd` session on the issue, and the failing check is its acceptance test: the same
recipe reads `PASS` when the fix is right.

## Cleanup

```bash
$V cleanup <run-id>
```

Sends TERM to every life this run started that is still alive, waits 15 s, then KILL. It signals
the recorded pid, after checking that pid still runs the engine module, so a pid the OS reused is
skipped. It never searches for engine processes by name. Stops and removes only the compose
services this run brought up. Scans evidence for the signing key. Deletes `scratch/`.
Keeps `evidence/`. Run it after every failed attempt too, so nothing strands a process or a port.

After cleanup, confirm the proof survived: `ls .agents/verify/<run-id>/evidence/`.

## Helpers

All in `.claude/skills/verify-tickwright/scripts/`, all executable:

| File | Invocation |
| --- | --- |
| `verify` | `.claude/skills/verify-tickwright/scripts/verify <verb> ...` (wrapper on the venv python) |
| `verify.py` | the helper itself, `verify --help` and `verify <verb> --help` |
| `run_strategy.py` | run by `verify start --strategy <name>`, never by hand |
| `strategies.py` | the four verification strategies `run_strategy.py` registers |

## Out of scope

Hyperliquid **mainnet** order placement. No preset wires `exchange=hyperliquid` with
`testnet=false`, and you must not build one.
