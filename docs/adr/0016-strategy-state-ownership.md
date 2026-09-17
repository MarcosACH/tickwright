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

**(Amended by [#233](https://github.com/MarcosACH/tickwright/issues/233): the read goes through
the `Cache`.** The high-water is still the saga store's, not the snapshot's. What changed is where
the bytes are read. The boot already deserializes every saga once, in `cache.rebuild()` (ADR-0024
step 2). The `StrategyHost` used to read `Store.all_orders()` a second time at step 6, which paid
the same mass read twice on the recovery path. It now folds over `Cache.all_orders()`, the
projection that rebuild filled. The startup reconciliation between the two steps only transitions
sagas the cache already holds and never touches `signal_id` or `cancel_signal_id`, so the fold sees
the same records the store read would.**)**

## Cadence

The engine snapshots strategy state on a configurable trigger (periodic + on `stop`, optionally
per `OrderEvent`), defaulting to periodic + on-stop. Because seq-safety comes from the saga
store, cadence only bounds how much *other* strategy state (indicators, counters) is
reconstructed after a crash; `extending.md` tells authors to keep state minimal and
reconstructible.

**(Amended by [#348](https://github.com/MarcosACH/tickwright/issues/348): the cadence is
per change, not periodic.** The `StrategyHost` saves a strategy's `snapshot()` after every
`on_tick` and `on_order_event` whose bytes differ from the last save, and again on `stop()`. There
is no timer and no config. A periodic cadence left a window: a single-shot strategy that fired and
was killed before the next tick fired again on restart and doubled the position. Cost is one
`snapshot()` call per callback and a store write only when the state moved, so a strategy whose
state moves on every tick pays one write per tick. That is the price of durability for that
strategy, and `extending.md` still tells authors to keep state small.

The snapshot write and the saga checkpoint are two writes, so one callback is still a crash
window. Which way it fails depends on the bus. On the in-memory bus a `Signal` published inside
`on_tick` waits in the FIFO until the callback returns, and the host writes the snapshot before
the drain reaches it. A crash between the two leaves a strategy that remembers firing and no
order: no trade, never a double. On Kafka the `Signal` is in the topic before the host writes, so
the same crash refires on restart. Closing that needs the snapshot in the same store transaction
as the `PENDING` checkpoint, which crosses the host and execution seam. Tracked as
[#350](https://github.com/MarcosACH/tickwright/issues/350).

A `snapshot()` that raises is not contained. ADR-0024 draws containment by handler origin, and
`snapshot()` is strategy code, but a snapshot the host cannot take is a failed checkpoint, which
ADR-0024 lists as an invariant class. Containing it would let the strategy trade on with nothing
durable, which is the double above waiting for the next crash. So the host probes `snapshot()` once
at `start()`, before any tick reaches the strategy, and a raise there or at any later save faults
the engine as an `InvariantViolation` naming the strategy.**)**

## Restore failure is not an invariant violation

An unreadable/incompatible snapshot (the strategy's code changed shape between runs) must not
fault the engine: on `restore()` failure the strategy **starts fresh**, with a warning and a
named event (`strategy.snapshot_incompatible`, ADR-0020). Seq-safety is unaffected — it comes
from the saga store, never the snapshot. `extending.md` recommends a version tag inside the
snapshot bytes.
