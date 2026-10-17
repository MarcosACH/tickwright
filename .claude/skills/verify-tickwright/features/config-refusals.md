# Config refusals

The engine refuses to start on a config it cannot run safely, with a readable message and exit
code 1. Three refusals matter most: a paper run with no genesis collateral, a genesis that
disagrees with the store's ledger, and a leverage entry for a symbol no strategy trades. Two more
came with safe exit: the reserved strategy id `__operator__`, and a store another engine holds.
An exit run (`cancel-all`, `flatten`) refuses with exit 2 instead. See `safe-exit.md`.

## Sub-features

- `refuse-genesis` no `TICKWRIGHT_PAPER__GENESIS_COLLATERAL` on a paper run.
- `refuse-mismatch` a different genesis against a store that already holds a ledger
  (`StoreAccountMismatch`, ADR-0043 §10).
- `refuse-dead-leverage` `TICKWRIGHT_LEVERAGE` names a symbol no strategy trades (ADR-0044 §3).
- `refuse-reserved` a strategy uses the id `__operator__` (ADR-0054).
- `refuse-lock` a second engine starts on a store a running engine holds (`StoreLockHeld`,
  ADR-0052).

## How to get to it (user POV)

- Edit `.env`, run `uv run tickwright`, read the error on stderr, check `$?`.

## Driving it with verify

Preconditions:

- Run id `refuse` is unused.

- **Set up.** Run `$V init refuse --feature config-refusals` and
  `$V ticks refuse ticks --row BTC:42000@0`.
- **No genesis.** Run
  `$V start refuse nogenesis --env TICKWRIGHT_REPLAY__PATH=ticks.jsonl --env TICKWRIGHT_SQLITE__PATH=store.db`,
  `$V await refuse nogenesis --exit`, then
  `$V check refuse nogenesis refuse-genesis --exit --expect 1`. The stderr file contains
  `exchange='paper' needs a starting collateral: set TICKWRIGHT_PAPER__GENESIS_COLLATERAL`.
- **Seed a ledger.** Run `$V start refuse first --preset paper-replay` and
  `$V await refuse first --event engine.feed_started`. Leave it running.
- **Lock held.** While `first` runs, run `$V start refuse locked --preset paper-replay`,
  `$V await refuse locked --exit`, then
  `$V check refuse locked refuse-lock --exit --expect 1` and
  `$V check refuse locked refuse-lock --event engine.faulted --expect 1`. The `engine.faulted`
  line's `error` starts with `StoreLockHeld` and names the holder's pid.
- **Stop the seed.** Run `$V signal refuse first TERM`, `$V await refuse first --exit`,
  `$V dump refuse first`.
- **Changed genesis.** Run
  `$V start refuse mismatch --preset paper-replay --env TICKWRIGHT_PAPER__GENESIS_COLLATERAL=50000`,
  `$V await refuse mismatch --exit`, then
  `$V check refuse mismatch refuse-mismatch --exit --expect 1` and
  `$V check refuse mismatch refuse-mismatch --event engine.faulted --expect 1`. The
  `engine.faulted` line's `error` starts with `StoreAccountMismatch`.
- **Dead leverage.** Run
  `$V start refuse deadlev --preset paper-replay --env 'TICKWRIGHT_LEVERAGE={"ETH": {"mode": "cross", "leverage": 3}}'`,
  `$V await refuse deadlev --exit`, then
  `$V check refuse deadlev refuse-dead-leverage --exit --expect 1`. The stderr file contains
  `leverage names symbols no configured strategy trades: ETH`.
- **Reserved id.** Run
  `$V start refuse reserved --preset paper-replay --env 'TICKWRIGHT_STRATEGIES=[{"kind":"single_shot_market","strategy_id":"__operator__","symbol":"BTC","side":"buy","quantity":"0.5"}]'`,
  `$V await refuse reserved --exit`, then
  `$V check refuse reserved refuse-reserved --exit --expect 1`. The stderr file contains
  `__operator__ is reserved for operator orders, not a strategy id`.
- **Untouched ledger.** Run
  `$V check refuse first refuse-mismatch --sql "select genesis_collateral from account" --expect 100000`.
- **Report.** Run `$V report refuse`. Expected verdict: `PASS (0 of 8 checks failed)`. The two
  alarm events listed are both `engine.faulted`, one for the lock and one for the mismatch. Both
  are expected.
- **Cleanup.** Run `$V cleanup refuse`.

## Gotchas

- A validation refusal is a pydantic traceback on stderr, not a JSON line. `--event` cannot
  match it. Grep the message text.
- A `StoreAccountMismatch` is a fault after the store opened, so it is a JSON `engine.faulted`
  line. Both shapes exit 1.
- The remedy for a mismatch is a fresh `TICKWRIGHT_SQLITE__PATH`, never editing the store.
