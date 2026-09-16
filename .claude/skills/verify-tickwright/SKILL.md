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
- Signals: `TERM` or `INT` stop gracefully (exit 0). `KILL` is a crash (exit -9, no snapshot).
  `USR1` trips the durable kill switch, `USR2` resets it. A config refusal or a fault exits 1.
- Two instances never share a store, a Kafka topic, or a scratch dir. The helper gives each run
  its own. Never drive `tickwright.db` or `.env` at the repo root. Those are the operator's.

## Launch

There is no long-lived server. Each drive is one or more short-lived **lives** of the process in
one **run**.

```bash
$V init <run-id>                                  # .agents/verify/<run-id>/{scratch,evidence}
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
service and records that this run owns it. Postgres gets a database per run and the helper prints
the DSN to pass as `TICKWRIGHT_POSTGRES__DSN`.

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
```

- `cloid` turns a signal id (`<strategy_id>:<symbol>:<seq>`) into the order's cloid, so you can
  wait on the store row for a specific order.
- `await` polls up to `--timeout` (default 30 s) and prints the matching log line or row. On
  timeout it prints the last five log lines and exits 1.
- A second life on the same run reuses `scratch/store.db`. That is how you verify a restart.
  Delete the file between lives when you want a fresh store.
- Two tick files in one run: `$V ticks <run> second --base 2024-01-01T00:00:05+00:00 ...` and
  `--env TICKWRIGHT_REPLAY__PATH=second.jsonl`.

Every recipe in `features/` uses only these verbs.

## Evidence

Everything a life produced lands in `.agents/verify/<run-id>/evidence/`:

| File | What it is |
| --- | --- |
| `run.json` | run id, sha, branch, start time, owned infra |
| `<life>.env.txt` | the exact `.env` the life ran with |
| `<life>.stderr.jsonl` | the engine's JSON log, complete |
| `<life>.exit` | the exit code |
| `<life>.signals.txt` | every start, signal and exit, timestamped |
| `<life>.store.txt` | `dump`: the store tables |
| `<life>.events.txt` | `dump`: event counts and the order, position, account, engine trail |
| `<name>.jsonl` | a copy of each tick file used |

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

## Cleanup

```bash
$V cleanup <run-id>
```

Sends TERM to every life this run started that is still alive (by recorded pid, checked against
the process command line, never by name), waits 15 s, then KILL. Stops and removes only the
compose services this run brought up. Scans evidence for the signing key. Deletes `scratch/`.
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
