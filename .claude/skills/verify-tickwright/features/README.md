# Tickwright verification map

This directory is the maintained source for verifying the engine as an operator runs it. Read this
index, then use the matching feature file as the recipe. `../SKILL.md` explains the helper.

## Baseline preconditions

- `V=.claude/skills/verify-tickwright/scripts/verify` and `$V doctor` says the package imports.
- Every recipe starts with `$V init <run-id>` and ends with `$V cleanup <run-id>`.
- Never drive the repo root `.env` or `tickwright.db`. The helper never touches them.
- The paper presets carry BTC and ETH specs with `max_leverage` 20 and frictionless fees. A recipe
  that needs fees or funding passes its own `TICKWRIGHT_PAPER__INSTRUMENT_SPECS`.
- The shipped strategies are `single_shot_market` and `single_shot_limit`, passed as JSON in
  `TICKWRIGHT_STRATEGIES`. Their cloids come from `$V cloid <strategy_id>:<symbol>:<seq>`.

## Driving conventions

- One `run-id` per feature attempt. A second life in the same run shares the store on purpose.
- Wait on the store row (`--order`, `--sql`) for anything that must be durable. Wait on the log
  (`--event`) for anything that is only observable there.
- Every life ends with `$V signal ... TERM`, `$V await ... --exit`, `$V dump ...`. A life you
  crashed with `KILL` still gets the `--exit` and the `dump`.
- Treat every command as literal. The JSON in `--env` is quoted with single quotes.

## Proof and skip reporting

- Every `Sub-features` id has at least one `check` in the recipe. The recipe's `report` line
  says the verdict the map expects today, including known FAILs named in Gotchas.
- A fill is proven by the `orders` row, the `positions` row, and the `account` row together.
- An exit code is proof. `0` is graceful, `1` is a refusal or a fault, `-9` is your `KILL`.
- Report the number you expected next to the number the dump shows.
- Report a feature you could not reach with the command you ran and the unmet precondition
  (Docker down, no key, no network). Never report a path as verified through a different path.

## Feature entry contract

Each file has an H1, one paragraph, then exactly four H2 sections in this order: `Sub-features`,
`How to get to it (user POV)`, `Driving it with verify`, `Gotchas`.

## Features

- [Paper round trip and restart](./paper-round-trip.md): the quickstart path, a market fill, a
  resting limit, a graceful stop, and a restart that recovers the fill and ghosts the limit.
- [Crash recovery](./crash-recovery.md): `KILL` mid-run, restart, the saga and ledger converge.
- [Kill switch](./kill-switch.md): `USR1` denies every new order, survives a restart, `USR2`
  clears it.
- [Ghost reconcile](./ghost-reconcile.md): a resting order the venue no longer holds is rejected
  after the 90 second grace window on the wall clock, and at the first tick on replay.
- [Paper economics](./paper-economics.md): taker fees, hourly funding, the seeded stochastic fill
  model with partial fills.
- [Config refusals](./config-refusals.md): the engine refuses to start on a missing genesis, a
  changed genesis, and a dead leverage entry, with a readable error and exit 1.
- [Portfolio strategies](./portfolio-strategies.md): four throwaway strategies that read the
  `Portfolio` seam, including margin at 5x and a two-symbol book.
- [Kafka bus](./kafka-bus.md): the same paper run over `KafkaBus`, messages counted on the topic.
- [Postgres store](./postgres-store.md): the same paper run on `PostgresStore`, restart included.
- [Hyperliquid feed](./hyperliquid-feed.md): mainnet public trades into the paper venue. No key.
- [Hyperliquid testnet](./hyperliquid-testnet.md): a real round trip on the testnet exchange, the
  account reconcile cadence, and key redaction.
