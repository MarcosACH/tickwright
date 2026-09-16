# Config refusals

The engine refuses to start on a config it cannot run safely, with a readable message and exit
code 1. Three refusals matter most: a paper run with no genesis collateral, a genesis that
disagrees with the store's ledger, and a leverage entry for a symbol no strategy trades.

## Sub-features

- `refuse-genesis` no `TICKWRIGHT_PAPER__GENESIS_COLLATERAL` on a paper run.
- `refuse-mismatch` a different genesis against a store that already holds a ledger
  (`StoreAccountMismatch`, ADR-0043 §10).
- `refuse-dead-leverage` `TICKWRIGHT_LEVERAGE` names a symbol no strategy trades (ADR-0044 §3).

## How to get to it (user POV)

- Edit `.env`, run `uv run tickwright`, read the error on stderr, check `$?`.

## Driving it with verify

Preconditions:

- Run id `refuse` is unused.

- **Set up.** Run `$V init refuse` and `$V ticks refuse ticks --row BTC:42000@0`.
- **No genesis.** Run
  `$V start refuse nogenesis --env TICKWRIGHT_REPLAY__PATH=ticks.jsonl --env TICKWRIGHT_SQLITE__PATH=store.db`
  and `$V await refuse nogenesis --exit`. Exit code `1`. `nogenesis.stderr.jsonl` contains
  `exchange='paper' needs a starting collateral: set TICKWRIGHT_PAPER__GENESIS_COLLATERAL`.
- **Seed a ledger.** Run `$V start refuse first --preset paper-replay`,
  `$V await refuse first --event engine.feed_started`, `$V signal refuse first TERM`,
  `$V await refuse first --exit`.
- **Changed genesis.** Run
  `$V start refuse mismatch --preset paper-replay --env TICKWRIGHT_PAPER__GENESIS_COLLATERAL=50000`
  and `$V await refuse mismatch --exit`. Exit code `1`. The log has one `engine.faulted` whose
  `error` starts with `StoreAccountMismatch`.
- **Dead leverage.** Run
  `$V start refuse deadlev --preset paper-replay --env 'TICKWRIGHT_LEVERAGE={"ETH": {"mode": "cross", "leverage": 3}}'`
  and `$V await refuse deadlev --exit`. Exit code `1`. The log contains
  `leverage names symbols no configured strategy trades: ETH`.
- **Proof.** Three `.exit` files read `1`. `$V dump refuse first` still shows the ledger at
  genesis `100000`, untouched by the two refused lives.
- **Cleanup.** Run `$V cleanup refuse`.

## Gotchas

- A validation refusal is a pydantic traceback on stderr, not a JSON line. `--event` cannot
  match it. Grep the message text.
- A `StoreAccountMismatch` is a fault after the store opened, so it is a JSON `engine.faulted`
  line. Both shapes exit 1.
- The remedy for a mismatch is a fresh `TICKWRIGHT_SQLITE__PATH`, never editing the store.
