# Write integrity: persist-before-publish, no outbox; Kafka is one topic keyed by symbol

Two decisions on the durable/distributed path, both validated against the established live-trading
reference (which relies on venue reconciliation, not an outbox or event-log replay).

## Persist-before-publish, and no transactional outbox

Per saga transition the `ExecutionManager` does two writes — checkpoint the saga to the `Store` and
publish the `OrderEvent` on the bus — which on `KafkaBus`+Postgres is a classic dual write. The rule:

1. **Checkpoint durably to the `Store`** (system-of-record, ADR-0009).
2. **Update the `Cache`** (write-through projection).
3. **Publish** the `OrderEvent`.

Never publish a transition not yet durably recorded — extending ADR-0008's write-ahead ethos to the
internal event, and mirroring the established practice of writing to the read-model before
publishing. A failed checkpoint is an `InvariantViolation` → `FAULTED` (ADR-0014).

**No transactional outbox.** A crash between (1) and (3) loses only the in-flight *bus* event. The
durable `Store` is truth; on restart the `Cache` rebuilds from it and **reconciliation heals against
the venue** (ADR-0009); idempotent `Order.apply()` makes any redelivery safe (ADR-0025). An outbox
exists to make the *bus* reliable — but Tickwright's thesis (ADR-0002) is that correctness **never**
depends on the bus, so an outbox is both philosophically inconsistent and redundant with
reconciliation. The Kafka `OrderEvent` stream is durable for external inspection/replay-for-debug,
but the engine never depends on it for recovery.

**Accepted residual risk** (ADR-0008 style): a crash in the checkpoint→publish window means a
`Strategy` may miss one `on_order_event`. The engine's money-affecting saga/cache state is always
healed (store + reconcile); only a strategy's *derived* view can lag, and strategies are already
contracted to keep state reconstructible (ADR-0016) and resume from snapshot + live ticks. A strategy
needing strict post-crash order state queries the read-model (a method call, ADR-0004) rather than
assuming every event arrived. Bounded and named, not hidden.

## Kafka is a single topic keyed by symbol

ADR-0003 promises the bus delivers a symbol's **whole causal chain (tick → signal → order → fill) on
a single ordered timeline**, and `InMemoryBus`'s drain-to-quiescence FIFO (ADR-0023) delivers exactly
that per-symbol total order. For backend parity `KafkaBus` must too, which requires a symbol's entire
chain on **one partition**: a **single `tickwright.events` topic keyed by `event.partition_key`**
(the symbol, ADR-0025), a **single consumer group** (ADR-0001 — no competing consumers / scale-out),
and a configurable partition count (partitions serve ordering here, not throughput). Consumers
deserialize via the boundary codec (ADR-0025) and dispatch by concrete type.

**Instance isolation (invariant).** Process-per-venue **and per-account** (ADR-0031, ADR-0038)
makes the topic name, the consumer-group id, and the store location (ADR-0019) **per-process
configuration** — two engine processes must never share any of the three. A shared topic would have
two venues consuming each other's events with **unqualified symbols colliding** (BTC exists on both
venues), silently re-importing the venue-identity problem ADR-0031 deferred; a shared store would
have two accounts' fills accreting onto one ledger, which ADR-0038's `account_id` check fail-fasts
on. The env keys already exist (`KAFKA_*_TOPIC`, `STORE_*`); this rule makes isolation a stated
invariant, not a deployment accident.

**Topic-per-family was rejected on parity grounds, not preference:** separate market/signals/orders
topics put a tick and the signal it caused on different partitions, so cross-family per-symbol order
is lost and the ADR-0003 delivery promise (and thus InMemory↔Kafka parity) breaks. The cost we accept
is that high-volume market ticks share the topic with low-volume order events; acceptable because
Kafka here proves durability/replay, not scale (ADR-0001), and latency is an explicit non-goal.
