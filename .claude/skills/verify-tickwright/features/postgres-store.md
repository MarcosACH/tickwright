# Postgres store

The same paper replay run on `PostgresStore`. The proof is a fill written to Postgres and a
second life that recovers it from there, with no SQLite file in the workspace.

## Sub-features

- `pg-schema` the store creates its tables on connect in a fresh database.
- `pg-fill` the fill, position and account rows land in Postgres.
- `pg-restart` life two restores from Postgres, clears the barrier, places nothing.

## How to get to it (user POV)

- `docker compose up -d postgres`, then `TICKWRIGHT_STORE=postgres uv run tickwright`.

## Driving it with verify

Preconditions:

- `$V doctor` says the Docker daemon is up.
- Run id `pg` is unused.

- **Infra.** Run `$V init pg` and `$V infra pg up postgres`. It prints the run's DSN,
  `postgresql://tickwright:tickwright@localhost:5432/verify_pg`. Export it as `DSN`.
- **Life one.** Run `$V ticks pg ticks --row BTC:42000@0 --row BTC:42100@1s`,
  `$V start pg first --preset paper-replay --env TICKWRIGHT_STORE=postgres --env TICKWRIGHT_POSTGRES__DSN=$DSN --env 'TICKWRIGHT_STRATEGIES=[{"kind":"single_shot_market","strategy_id":"shooter","symbol":"BTC","side":"buy","quantity":"0.5"}]'`,
  `$V await pg first --order $($V cloid shooter:BTC:1)=FILLED`, `$V signal pg first TERM`,
  `$V await pg first --exit`.
- **Life two.** Run the same `start` line with life `second`,
  `$V await pg second --event engine.feed_started`, wait one second, `$V signal pg second TERM`,
  `$V await pg second --exit`, `$V dump pg second`.
- **Proof.** `ls .agents/verify/pg/scratch` has no `store.db`. `second.store.txt` shows the one
  `filled` order and `shooter | BTC | 0.500`. `second.events.txt` has no `order.placed`.
- **Cleanup.** Run `$V cleanup pg`. It removes the container, and the database with it.

## Gotchas

- `await --order` and `dump` read Postgres through the DSN in the life's `.env`. They answer "no
  rows" until the store has created its schema, so an early await is fine.
- The DSN is per run. Never point two runs at one database, and never at the default
  `tickwright` database the compose file documents.
- The `postgres` pytest tier is a different check (`STORE_POSTGRES_DSN=... uv run pytest -m postgres`).
  This recipe drives the app, not the suite.
