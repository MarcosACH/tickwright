# Strategy state: engine-persisted into the shared store; seq recovered from the saga store

The `Strategy` owns its state *content* via `snapshot() -> bytes` / `restore(bytes)`. The
**engine persists** those bytes into the **same durable store** as the order saga — there is no
separate `StateStore` Protocol (that would be a second persistence seam pushing durability onto
strategy authors, exactly what a reference engine should handle for them). This mirrors
the established `save`/`load` + engine-persists model.

## Seq-safety is independent of snapshot freshness

On the live path a crashed strategy **cannot replay ticks** (the market moved on), so it resumes
from current ticks. If the `signal_id` seq were restored only from a possibly-stale strategy
snapshot, the strategy would re-emit an already-used seq for a *new* intent and the
`ExecutionManager` would drop it as a duplicate. Therefore the **seq high-water-mark is recovered
from the saga store**, not the snapshot: the Manager checkpoints every order keyed by
`signal_id` (ADR-0008), so the store knows the highest seq ever consumed per strategy, and
restart sets `next_seq = max(persisted seq) + 1`. This makes seq-safety robust even if the
strategy snapshot is stale.

## Cadence

The engine snapshots strategy state on a configurable trigger (periodic + on `stop`, optionally
per `OrderEvent`), defaulting to periodic + on-stop. Because seq-safety comes from the saga
store, cadence only bounds how much *other* strategy state (indicators, counters) is
reconstructed after a crash; `extending.md` tells authors to keep state minimal and
reconstructible.
