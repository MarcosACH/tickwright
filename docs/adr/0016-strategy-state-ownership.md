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
`signal_id` (ADR-0008) **and every cancel intent keyed by its own `signal_id`** (ADR-0026 —
cancels consume seqs too; omitting them would let a restart reuse a consumed id), so the store
knows the highest seq ever consumed per strategy, and restart sets
`next_seq = max(persisted seq) + 1`. `seq` is **one per-strategy monotonic counter across all
symbols** (the `{symbol}` in the id is routing, not a second counter scope), so the HWM is a
single max over the strategy's records. This makes seq-safety robust even if the strategy
snapshot is stale.

## Cadence

The engine snapshots strategy state on a configurable trigger (periodic + on `stop`, optionally
per `OrderEvent`), defaulting to periodic + on-stop. Because seq-safety comes from the saga
store, cadence only bounds how much *other* strategy state (indicators, counters) is
reconstructed after a crash; `extending.md` tells authors to keep state minimal and
reconstructible.

## Restore failure is not an invariant violation

An unreadable/incompatible snapshot (the strategy's code changed shape between runs) must not
fault the engine: on `restore()` failure the strategy **starts fresh**, with a warning and a
named event (`strategy.snapshot_incompatible`, ADR-0020). Seq-safety is unaffected — it comes
from the saga store, never the snapshot. `extending.md` recommends a version tag inside the
snapshot bytes.
