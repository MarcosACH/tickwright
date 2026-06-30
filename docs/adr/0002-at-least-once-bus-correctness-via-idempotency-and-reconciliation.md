# EventBus is at-least-once; correctness lives in idempotency + reconciliation, not the transport

The `EventBus` interface promises only **at-least-once** delivery: every event is seen at
least once, duplicates are legal, consumers MUST be idempotent. The contract is identical on
both backends — `InMemoryBus` is synchronous in-loop dispatch (deterministic, effectively
exactly-once on the happy path) but is *held to* at-least-once so consumers are written one
way; `KafkaBus` is genuinely at-least-once over the network. "Exactly-once" is never a
transport claim — it is an **emergent property** of idempotent consumers plus exchange
reconciliation.

**Load-bearing rule (do not erode):** engine correctness must never *depend* on bus delivery
guarantees. The two real safety nets are (1) deterministic idempotency keys that let any
consumer dedupe a replayed event, and (2) periodic reconciliation against the exchange as the
ultimate source of truth. A future change that "optimizes" by trusting the bus (e.g. assuming
no duplicates, or Kafka EOS transactions) reintroduces the exact class of state-corruption
this rule prevents.

## Considered options

- **Exactly-once illusion** (rejected): Kafka EOS transactions / a no-duplicate promise.
  Heavy, leaky, and it lets recovery code assume "I'll never reprocess an event" — the
  assumption that corrupts state the first time a process dies between handling and commit.

## Why (evidence)

Battle-tested engines converge here. A single-threaded in-process bus gives **no** delivery
guarantee and no in-flight crash recovery; correctness comes from deterministic IDs +
domain-level idempotent application (`Order.apply()` enforces each `trade_id` once;
`is_duplicate_fill()` skips dupes) + venue reconciliation (parallel WS-fills and fill-history
polling that synthesizes corrective fills). The author's prior system reached the same answer
independently: idempotent fill-write via a `filltid` unique constraint + a ghost reconciler +
a fill-history backstop. See ADR-0001 for the single-process runtime this contract rides on.
