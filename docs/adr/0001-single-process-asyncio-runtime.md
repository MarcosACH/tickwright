# Single-process asyncio runtime; EventBus is a transport, not a topology

The whole pipeline (feed → strategy → exchange → engine) runs as cooperating `asyncio`
tasks in **one process with one event loop**, mirroring a proven single-threaded
deterministic core. The `EventBus` is a swappable *transport* behind one interface:
`InMemoryBus` is direct in-loop dispatch, `KafkaBus` is the **same topology** publishing
through Kafka topics for durability, replay, and cross-process inspectability. Swapping the
backend never forks the runtime into multiple worker processes.

We chose this over a backend-defined topology (in-memory = one process, Kafka = N worker
processes, matching the author's prior production system) because a forked runtime would
mean two recovery models, two ordering stories, two test harnesses, and would surrender the
determinism that makes the engine trivially testable and readable in an afternoon — the
entire value proposition. The cost we accept: v1 does **not** demonstrate horizontally
scaled multi-worker consumption (competing consumers, partition rebalancing); Kafka here
proves durability/replay, not scale-out.

## Consequences

- The `Engine` runner is shaped so a future multi-process split is an additive change, not a
  breaking one (the bus interface already hides the transport).
- "Two implementations per seam" is honored as **two transports, one topology**.
