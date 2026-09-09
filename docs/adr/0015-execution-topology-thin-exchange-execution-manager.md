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

**(Amended by [#195](https://github.com/MarcosACH/tickwright/issues/195)'s
`/improve-codebase-architecture` review: `Exchange` is now composed of its two anchors, and
"thin" is a claim about the adapter that has to be one about the caller too.** The seam reached
ten members, and its four engine consumers used **disjoint** subsets of them — the
`ExecutionManager` `place`/`cancel`, the `Reconciler` `fetch_order`, `LedgerReconciliation`
`fetch_account_state`/`verify_account_mode`, the runner the lifecycle and the two declarations.
Every one of them depended on all ten, so each widening obliged every caller and every double to
learn a grain it never read; `verify_account_mode` was the sixth such widening and the one that
made the cost legible, since three of its four implementations are a constant.

The split is **by anchor**, which is this repo's own division and not a new one: ADR-0034 runs two
reconciliation cycles on two anchors, and the members partition along them exactly. `OrderAnchor`
is the venue keyed by **cloid** — `place`, `cancel`, `fetch_order` — and `AccountAnchor` is the
venue keyed by the **account** — `fetch_account_state`, `verify_account_mode`. What is left on
`Exchange` is what answers for the venue itself: `start`/`run`/`stop` and the two static
declarations. Commands sit with a query on `OrderAnchor` deliberately — a placement's outcome is
learned by reading it back, so a caller holding one without the other could not close the loop —
and the lifecycle is *not* made a third anchor, because no caller narrows to it: the runner holds
the composite either way, being the one thing that hands the anchors out.

**Nothing about an adapter changes, and that is the point.** Both adapters satisfy the whole seam
structurally, as before; `isinstance(adapter, Exchange)` and the per-adapter seam-claims gates walk
all ten members unchanged, since `get_protocol_members` includes inherited ones. What changed is
what a *caller* declares. The narrowing is not merely notation: the account cycle's own double went
from a subclass of the shared ceremony base with three assertion-raising stubs to two members and
no base at all, which converts "the account cycle places nothing" from a runtime assertion into a
type error. `tests/domain/test_protocols.py` pins the partition, so a member added later lands on
the anchor its callers read rather than drifting back onto the composite one method at a time.

This does not thicken or thin the adapter in the sense this ADR means, and it does not touch the
saga's single owner below.**)**

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

The `Exchange.fetch_*` methods answer a failed read with something that is never venue truth (the
ADR-0011 connectivity guard, expressed in the type signature), and the grain decides with what:
`fetch_order` returns a `VenueReadFailure` — never a view — whose member says *which way* the read
failed, because the reconciler behind it drives a worklist and acts on the difference;
`fetch_account_state` reads one grain with nothing behind it to spare and collapses both to `None`
([ADR-0049](./0049-failed-read-blast-radius.md)). This also partly settles the data-path-vs-execution-path
question (Open Q#18): the `Strategy`/`ExecutionManager` seam *is* our data/execution split,
without importing a full DataEngine/ExecutionEngine surface.
