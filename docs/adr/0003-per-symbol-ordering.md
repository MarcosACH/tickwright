# EventBus guarantees per-symbol ordering only

The `EventBus` promises ordered delivery **per symbol (per instrument)** and nothing wider.
Ticks, signals, and order-lifecycle events are all keyed by symbol, so one symbol's whole
causal chain (`tick → signal → order → fill`) stays on a single ordered timeline. Cross-symbol
ordering is explicitly **not** guaranteed.

This is the smallest scope the engine actually needs: an order's lifecycle must be causally
ordered and a symbol's ticks must be monotonic, but a BTC fill has no causal relation to an
ETH fill, and reconciliation is order-insensitive. Promising exactly this and no more keeps
both backends honest: `KafkaBus` achieves it with symbol as the partition key (its strongest
practical guarantee), and `InMemoryBus`'s total global order is a strict superset that
satisfies it trivially — so parity holds.

**Load-bearing rule:** the engine must never rely on cross-symbol ordering; this is a standing
property-test invariant.

**Caveat (deferred scope).** Per-symbol keying covers ticks, signals, and order-lifecycle
events — all instrument-scoped. *Account-level* events (e.g. an `AccountState`
balance snapshots) are account-scoped, not symbol-scoped, and would need their own ordering key
(account). v1 is engine-only with portfolio/accounting deferred, so no such event exists yet;
if one is introduced, it must not be forced onto the per-symbol key.

## Considered options

- **Per-order-id ordering** (rejected): finer-grained and more Kafka parallelism per symbol,
  but ticks (symbol-keyed) and an order's lifecycle (order-keyed) land on different
  partitions, so the tick→order→fill causal chain spans partitions — harder to read and
  reason about, with no benefit under ADR-0001's no-scale-out stance.
- **Global total ordering** (rejected): would force a single Kafka partition, defeating the
  backend's purpose, for cross-symbol ordering the engine never uses.
