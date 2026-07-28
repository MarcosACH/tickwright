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
  `oid`, reason codes, cancel intent (`cancel_requested` + the cancel's own `signal_id`,
  ADR-0026).
- **Strategy snapshots**: opaque bytes per `strategy_id`.
- **Kill-switch state**: durable and sticky (ADR-0026), restored before the feed starts.
- **The position ledger**, keyed by `(strategy_id, symbol)`: signed size, entry price, realized
  PnL, accrued fees, accrued funding, isolated collateral (ADR-0043 §3).
- **The account row**: a single row by constraint (one process trades one account, ADR-0038)
  holding `account_id`, the opening declaration — genesis collateral (ADR-0042) and the instant it
  was written — and the cash line.
- **The funding watermarks**, one row per symbol: the last funding boundary applied to it
  (ADR-0043 §5.2) — at `symbol` grain because that is the grain of ADR-0037's key.

The last three are current-state rows, not an event log: recovery is a `SELECT`, and "snapshot"
means the current row state (ADR-0043 §1, extending ADR-0009 from orders to accounting).

## One error contract for the seam

**Every member of the Protocol either reaches durable storage or raises `InvariantViolation`** —
reads included, and a backend's own exception type is never part of the contract. This *widens*
ADR-0014, which names "a failed checkpoint **write**"; the widening is recorded here, where the
seam is decided, rather than left to the adapters.

It is a property of the **seam**, not of whichever method the caller happened to touch. The type is
load-bearing: `InvariantViolation` is what pierces the engine's strategy-containment net and faults
the run (ADR-0024), so a raw `sqlite3.Error` or `psycopg.Error` crossing here is filed as a caller's
bug and *survived*, while the process has in fact lost the ability to make its state durable. One
contract also means a store call can move under the containment net later without its failure mode
changing shape — which is the whole reason a read is covered and not just a write.

Each adapter contributes exactly one thing to the rule: the base class its driver raises from
(`sqlite3.Error`, `psycopg.Error`). Both drivers expose one, so the translation itself has no
per-backend half.

**`close()` is the one deliberate exception**: teardown that fails is a leaked resource, which the
runner already records as a stop-hook failure, not a claim about durability that turned out false.
The exclusion is asserted rather than assumed — the store contract suite checks its call table
against `get_protocol_members(Store)`, so a member added without the translation fails loudly
instead of silently narrowing the rule.

The seq high-water-mark is **derived** from saga records (no separate table). There is **no
"processed event id" table** — dedup is enforced by idempotent `Order.apply()` (ADR-0025); Kafka
consumer offsets merely bound how much is redelivered, and the in-memory path has no redelivery.
**The ledger honors this rather than excepting it**: the saga's applied-event set is
authoritative for the ledger too, because the ledger write shares the saga's transaction
(ADR-0043 §4), and funding — the one ingress with no saga to ride — is deduped across a restart by
a **watermark, never by a set of processed ids**. `funding_marks` is a table, but not the banned
kind: the rule forbids a record that grows with the event stream, and this one holds a single
overwritten row per traded symbol, bounded by the symbols the account has ever held — configured or
arrived as foreign flow — rather than by history (ADR-0043 §5.2). **Both paths write it and both
read it**: it gates live's re-delivered history and a replay rerun's re-derived boundaries alike,
and is simply inert under live-wall-clock paper, whose generator only moves forward (ADR-0043 §5.1).
The store location is per-process configuration, never shared between engine instances (ADR-0028).
