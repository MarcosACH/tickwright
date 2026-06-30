# v1 supports multi-symbol and a multi-strategy runtime, scalable to N

v1 runs **multiple symbols** and **multiple concurrent strategies** (N, configurable — no fixed
cap). The `strategy_id` keying (`signal_id = {strategy_id}:{symbol}:{seq}`, ADR-0006) already
anticipates this, so the cost is contained to the `ExecutionManager` gaining:

- a **strategy registry** with **unique-`strategy_id` enforcement** (duplicate registration is a
  fail-fast error),
- **per-strategy `OrderEvent` routing** (events return to the owning strategy),
- **per-strategy seq + snapshot** (already implied by ADR-0006/0016).

Multiple strategies may trade the **same symbol** independently; their orders stay isolated by
distinct `cloid`s, and per-symbol ordering (ADR-0003) is unaffected.

## Scope guard: engine capability, not a strategy library

The deliverable is the **multi-strategy engine**, not a catalog of algorithms. The shipped
strategies remain **minimal reference impls** (e.g. SMA-cross, grid) — exactly **two** prove the
`Strategy` seam; the runtime then runs N *instances*. This honors the README non-goal ("not a
strategy marketplace / indicator library") while fully delivering multi-strategy support;
growing the *number of distinct algorithms* is explicitly out of scope. The tracer-bullet stays
single-symbol / single-strategy (the thinnest end-to-end slice); tests cover multi-symbol and
multi-strategy.
