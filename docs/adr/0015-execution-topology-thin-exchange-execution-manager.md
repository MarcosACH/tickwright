# Execution topology: thin Exchange adapters + a single ExecutionManager owning the saga

The four swappable seams are `Protocol`s: **`MarketFeed`** (`HyperliquidFeed`, `ReplayFeed`),
**`Strategy`**, **`Exchange`** (`PaperExchange`, `HyperliquidExchange`), **`EventBus`**
(`InMemoryBus`, `KafkaBus`). The **`ExecutionManager`** is deliberately **not** a Protocol — it
is the one engine-internal orchestrator, not a swappable seam.

Uniform bus coupling:

```
MarketFeed ─publish MarketTick─▶ [bus] ─▶ Strategy.on_tick
Strategy ───publish Signal─────▶ [bus] ─▶ ExecutionManager
ExecutionManager ─place()/cancel()─▶ Exchange ─raw ExecutionReport─▶ [bus]
ExecutionManager ─(advance saga, checkpoint)─ publish OrderEvent ─▶ [bus] ─▶ Strategy.on_order_event
```

## The saga lives in one place

The `Exchange` is a **thin boundary adapter** that only translates venue ↔ our types
(`place`, `cancel`, `fetch_*`) and emits **raw venue facts** as `ExecutionReport`s. The
`ExecutionManager` subscribes to `Signal`s and `ExecutionReport`s and owns **cloid assignment +
checkpoints + the FSM**, publishing the canonical `OrderEvent`s strategies consume. The
crash-safe saga is written **once** and serves Paper and Hyperliquid identically — mirroring
the established `ExecutionEngine` (state/saga) vs `ExecutionClient` (venue adapter) split. The
alternative (each `Exchange` owns its own saga) was rejected: it duplicates the hardest logic
per venue, can't be unit-tested without a venue, and lets the Paper and live paths drift.

## Two-layer event model

Raw **`ExecutionReport`** (venue fact: acked/filled/rejected cloid) from the `Exchange` →
canonical **`OrderEvent`** (saga transition) from the `ExecutionManager`. This keeps all FSM
decision logic in the Manager, puts inspectable raw venue facts on the bus (Kafka-replayable for
debugging), and gives reconciliation's synthetic events a natural origin (the Manager publishes
them, `reconciliation`-flagged).

The `Exchange.fetch_*` methods return `None` on failure (the ADR-0011 connectivity guard,
expressed in the type signature). This also partly settles the data-path-vs-execution-path
question (Open Q#18): the `Strategy`/`ExecutionManager` seam *is* our data/execution split,
without importing a full DataEngine/ExecutionEngine surface.
