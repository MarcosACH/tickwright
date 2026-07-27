# Execution topology: thin Exchange adapters + a single ExecutionManager owning the saga

The four swappable seams are `Protocol`s: **`MarketFeed`** (`HyperliquidFeed`, `ReplayFeed`),
**`Strategy`**, **`Exchange`** (`PaperExchange`, `HyperliquidExchange`), **`EventBus`**
(`InMemoryBus`, `KafkaBus`). The **`ExecutionManager`** is deliberately **not** a Protocol — it
is the one engine-internal orchestrator, not a swappable seam.

Each seam **accepts N implementations**; the two shipped per seam only *prove* the seam (the
ADR-0018/0001 framing), they do not cap it. For **`Exchange`** specifically, each real venue is a
**self-contained adapter module** owning its venue translation, reconciliation queries,
instrument-spec sourcing, and — since ADR-0044 — the boot-time **account-configuration sync**
(pushing the configured per-symbol leverage and margin mode to the venue in `start()`), so adding a
venue is an additive module plus a process — never a core change. That sync does not thicken the
adapter in the sense this ADR means: it is startup config alignment, not saga. The adapter still
owns no order state, and the write happens once, before the barrier, on a path no order crosses.

The venue-extensibility model (process-per-venue, adapter self-containment, instrument-spec
wiring) is specified in ADR-0031; package topology and the composition root in ADR-0032.

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
