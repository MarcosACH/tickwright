# Durable store: a Store Protocol, SQLite default + Postgres second, paired with the bus

Crash-safe state lives behind a `Store` Protocol with **two implementations**, mirroring the
EventBus split (ADR-0001):

- **`SQLiteStore` (default)** — zero-setup, in-process, real SQL; a file for durability or
  `:memory:` for tests. Paired with `InMemoryBus` it gives the **zero-external-services default
  path** — the engine runs and recovers with nothing installed (the "runnable in an afternoon"
  promise).
- **`PostgresStore` (second)** — production parity, paired with `KafkaBus` for the distributed
  story (what real deployments, and the author's prior system, run).

So the canonical pairings are **InMemoryBus + SQLite** (zero-setup, deterministic) and
**KafkaBus + Postgres** (distributed/production parity).

## What the store holds (minimal)

- **Order saga records**, keyed by cloid: state, transition history, send timestamp, venue
  `oid`, reason codes.
- **Strategy snapshots**: opaque bytes per `strategy_id`.

The seq high-water-mark is **derived** from saga records (no separate table). There is **no
"processed event id" table** — bus-redelivery dedup rides on Kafka consumer offsets on the Kafka
path and does not arise on the in-memory path.
