# Recovery is snapshot-plus-reconcile; the cache is a write-through projection

The durable order store (the ADR-0008 saga checkpoints) is the **system-of-record** for order
state. The in-memory **Cache is a write-through projection**, rebuilt from the store on
restart — never the source of truth. Recovery is: **restore snapshot from the store →
reconcile each non-terminal order by cloid against the venue → resume sagas.** Strategy state
restores from its own snapshot (ownership decided separately). The `EventBus`/event log is
**propagation only**.

We reject event-sourced replay as the live recovery path. It would force a durable event log
onto the `InMemoryBus` (which has none), breaking the ADR-0001/0002 parity guarantee or adding
a second recovery model. The README's "idempotent event replay → reconstruct state" is
**reframed**: we keep idempotent event *application* (`apply(event)` converges — bus
redeliveries and replays are safe), a correctness property, **not** a recovery mechanism.

This matches established best practice for live trading engines: the cache is a write-through
projection, not the source of truth — it answers "what is true now," while the durable store
answers "how did we get here." Live restart uses snapshot-plus-reconcile, **not** event replay.
A durable event-capture log, where present, is a separate, optional
audit/verification/deterministic-backtest capability (run manifests, hash verification), not
the live system-of-record.

## Consequences

- Layering is **durable store (truth) → Cache (write-through projection) → bus (propagation)**,
  with reconciliation as the healer between store and venue.
- An optional durable event-capture/audit capability may be added later; it never becomes the
  live recovery path in v1.
