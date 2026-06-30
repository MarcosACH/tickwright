# Tickwright

The event-driven core of an algorithmic trading engine: it turns a market feed into orders
through `MarketFeed → Strategy → Exchange`, coordinated by an `EventBus`, with a crash-safe
order-lifecycle saga, idempotent recovery, and exchange reconciliation. A readable reference
implementation, not a product. Apache-2.0 licensed.

## Language

**Engine**:
The single `asyncio` process that hosts the whole pipeline on one event loop and owns the
order-lifecycle saga, reconciliation, and recovery. There is exactly one runtime topology;
the [[EventBus]] backend never changes it. See ADR-0001.
_Avoid_: runner, node, worker, service (the prior system's multi-process "workers" are not
this).

**Event**:
An immutable fact published on the [[EventBus]] (`MarketTick`, `Signal`, `OrderPlaced`,
`OrderFilled`, `OrderRejected`, `OrderCancelled`, …). The system's only currency; never
mutated after dispatch (replay/audit depend on stability).
_Avoid_: message, record (reserve "message" for the transport-level envelope).

**EventBus**:
The transport every component couples through — publish/subscribe by event type. A swappable
*backend* (`InMemoryBus` = in-loop dispatch, `KafkaBus` = same topology over Kafka topics for
durability/replay), **not** a process-topology switch. Delivery is **at-least-once**
(duplicates legal, [[Idempotent consumer|consumers must be idempotent]]) and ordered
**per symbol only** (every event keyed by symbol; cross-symbol order is not guaranteed). The
interface is **pub/sub only** (`publish`/`subscribe`); query-shaped reads (e.g. reconciliation
reading the exchange) are direct Protocol method calls, not bus messages. See
ADR-0001, ADR-0002, ADR-0003, ADR-0004.
_Avoid_: message queue, broker, channel (those imply a specific backend).

**Idempotent consumer**:
A subscriber that converges to the same state whether it sees an [[Event]] once or many
times, by deduping on a deterministic idempotency key. The engine's correctness rests on
this plus [[Reconciliation]] — **never** on the [[EventBus]] delivery guarantee. See
ADR-0002.
_Avoid_: exactly-once consumer (we never promise exactly-once at the transport).

**Order saga**:
The explicit state machine that governs one order's life: `PENDING → SUBMITTED → LIVE →
PARTIALLY_FILLED → FILLED`, with terminals `CANCELLED`/`DENIED`/`REJECTED`/`FAILED`. Keyed by
[[Client order id|cloid]], crash-recoverable, idempotent on re-applied terminal transitions.
A timeout never transitions it — only [[Reconciliation]] moves a stuck `SUBMITTED`. The
`PENDING` record is written **before** the network send (write-ahead intent), and recovery
**reconciles by cloid before any resend**. See ADR-0007, ADR-0008.
_Avoid_: order state machine (fine informally), workflow, process.

**DENIED** vs **REJECTED** vs **FAILED**:
The three negative terminals, split on "was it sent, and who decided?". `DENIED` = our
pre-trade guard refused it, **never sent** (safe to recreate). `REJECTED` = **sent**, the
**venue** refused it (includes ghost-reconciled). `FAILED` = **sent**, we proved it **never
landed**. Never collapse them — their recovery differs. See ADR-0010.
_Avoid_: error, cancelled (a cancel is none of these).

**MarketFeed** *(Protocol)*:
Produces [[MarketTick]] events for configured symbols. Impls: `HyperliquidFeed`, `ReplayFeed`
(deterministic, file-backed — for tests/dev, not a backtester). See ADR-0015.
_Avoid_: data source, provider, feed handler.

**Strategy** *(Protocol)*:
Consumes [[MarketTick]]s (`on_tick`), reacts to lifecycle (`on_order_event`), and emits
[[Signal]]s with deterministic [[signal_id]]s. Owns its state *content* via `snapshot()`/`restore()`; the engine persists those bytes into the
shared durable store. Its [[signal_id]] seq high-water-mark is recovered from the **saga store**
(not the snapshot), so it is robust to stale snapshots. v1 runs **N strategies concurrently**
(unique `strategy_id`, per-strategy routing/seq/snapshot); the shipped strategies stay minimal
reference impls (engine capability, not a library). See ADR-0006, ADR-0015, ADR-0016, ADR-0018.
_Avoid_: algo, bot, trader.

**Exchange** *(Protocol)*:
A **thin boundary adapter** translating venue ↔ our types: `place`/`cancel`/`fetch_*` and
emitting raw [[ExecutionReport]]s. Owns no saga. `fetch_*` returns `None` on failure (the
[[Connectivity guard]]). Impls: [[Paper exchange|PaperExchange]], `HyperliquidExchange`. See
ADR-0011, ADR-0015.
_Avoid_: broker, venue client, gateway (fine informally).

**ExecutionManager**:
The single engine-internal orchestrator (not a Protocol) that owns the [[Order saga]]: it
subscribes to [[Signal]]s and [[ExecutionReport]]s, assigns the [[Client order id|cloid]],
checkpoints, drives the FSM, and publishes canonical `OrderEvent`s. The saga is written once and
serves every [[Exchange]]. See ADR-0015.
_Avoid_: execution engine (fine informally), order manager, router.

**ExecutionReport** vs **OrderEvent**:
The two event layers. An **ExecutionReport** is a *raw venue fact* the [[Exchange]] emits
("venue acked/filled/rejected cloid X"). An **OrderEvent** is the *canonical saga transition*
the [[ExecutionManager]] publishes after applying that fact. See ADR-0015.
_Avoid_: using them interchangeably — one is venue truth, one is engine state.

**Signal**:
An [[Event]] a [[Strategy]] emits expressing an order intent (place/cancel). Carries a
deterministic [[signal_id]] so replays converge. The `Exchange` consumes signals; the engine
turns each into an order saga.
_Avoid_: order request, command (the bus has no command pattern — a signal is just an event).

**signal_id**:
The deterministic identity of a [[Signal]], `{strategy_id}:{symbol}:{seq}` with `seq` a
strategy-owned monotonic counter restored from snapshot on restart. The engine's saga and
dedup are keyed on it. **Must be a pure function of strategy state — never random.** See
ADR-0006.
_Avoid_: signal uuid, request id.

**Client order id** / `cloid`:
The exchange-facing order identity (Hyperliquid `cloid`: a 128-bit hex string), derived
deterministically from [[signal_id]]. Stable handle for cancel-by-cloid and reconciliation
matching. The engine — not the venue — is the dedup authority. See ADR-0006.
_Avoid_: order id (reserve that for the exchange-generated id), oid.

**Clock**:
The injected source of all time — reads (`now`/`timestamp_ns`), waits (`sleep`), and timers.
Engine code never touches `asyncio.sleep` or `time.time()` directly. `LiveClock` uses the
wall clock and real async waits; `ManualClock` advances virtual time explicitly so tests are
deterministic and never sleep. Canonical timestamp is UTC epoch nanoseconds. See ADR-0005.
_Avoid_: timer, scheduler (those are facets of the Clock, not separate concepts).

**Store** (durable store) *(Protocol)*:
The system-of-record behind the [[Cache]]: holds order saga records (keyed by
[[Client order id|cloid]]) and [[Strategy]] snapshots. Impls: `SQLiteStore` (default,
zero-setup) + `PostgresStore` (production parity). Paired with the [[EventBus]] backend —
InMemory+SQLite or Kafka+Postgres. See ADR-0019.
_Avoid_: database, persistence layer (fine informally), repository.

**Cache**:
The in-memory read-model of current order/position state — a **write-through projection** of
the durable order store, not the source of truth. Rebuilt from the store on restart. Answers
"what is true now"; the store answers "how did we get here." See ADR-0009.
_Avoid_: source of truth, database (the durable store is the truth; the cache projects it).

**Recovery** (snapshot-plus-reconcile):
On restart: restore the [[Cache]] from the durable store, [[Reconciliation|reconcile]] each
non-terminal order by [[Client order id|cloid]] against the venue, resume sagas. Event replay
is **not** the recovery path — only idempotent event *application* converges. See ADR-0009.
_Avoid_: event replay, event sourcing (those are a deferred audit capability, not recovery).

**Reconciliation**:
The healer that periodically compares local order state against the venue's truth and applies
the difference, in two phases (startup mass-rebuild, continuous loops) and two cadences (fast
in-flight check, slower open-order/ghost reconcile). The correctness net under at-least-once
delivery and crash recovery. See ADR-0011.
_Avoid_: sync, refresh, polling (those undersell the heal-against-truth role).

**Connectivity guard** (`None`-not-`[]`):
The invariant that a failed venue read returns `None`, never `[]`; on `None`, [[Reconciliation]]
**freezes** the cycle and removes nothing. An outage must never be misread as "all orders
vanished." See ADR-0011.
_Avoid_: empty result, no orders (the whole point is that these differ from a failure).

**Ghost** / ghost-reconciled:
An order absent from the venue continuously across the grace window. Only after a fill-history
cross-check (it may have filled) is it resolved: `REJECTED` if truly gone, `FILLED` if fills
are found. A ghost is an *order* the reconciler removes — distinct from a duplicate/stale
*fill*. See ADR-0011.
_Avoid_: orphan, stale order, dead order.

**Synthetic event**:
A lifecycle [[Event]] the reconciler generates (a healed fill, a ghost rejection) rather than
the venue pushing it. Carries a deterministic id and a `reconciliation` flag so it is
idempotent on replay and auditable as reconciler-sourced. See ADR-0011.
_Avoid_: fake event, manual event.

**Paper exchange** / `PaperExchange`:
The in-process deterministic `Exchange` impl and the default v1 target. Holds a book of resting
LIMIT orders, fills MARKET on receipt against the latest [[MarketTick]], re-checks limits each
tick. Frictionless — emits price + quantity, no fees/margin/PnL. See ADR-0012, ADR-0013.
_Avoid_: simulator, mock exchange, backtester (it is a live/paper venue, not a backtest engine).

**Fill model**:
The swappable seam deciding whether/at what price an order fills inside the [[Paper exchange]].
`ImmediateFillModel` (default) is deterministic, optimistic, zero-slippage, full-fill;
`StochasticFillModel` adds seeded-random queue/slippage/partials/latency. Both take an injected
RNG and [[Clock]], so fills are deterministic in tests. See ADR-0012.
_Avoid_: matching engine (fine informally), execution model.

**PreTradeGuard** *(seam)*:
The thin pre-trade check the [[ExecutionManager]] runs before placing: min-notional,
quantity/price validity, kill-switch. Failure → `DENIED`. Impls: a real guard + `NoopGuard`.
**Not** a RiskEngine (no exposure/position limits — deferred). See ADR-0017.
_Avoid_: risk engine, risk manager (those imply the deferred portfolio-risk surface).

**Quantization**:
Rounding an order's price to the instrument tick and size to its lot/step at the boundary, so
the venue never silently rejects it. Uses [[Instrument spec]]s. See ADR-0017.
_Avoid_: rounding (too generic).

**Instrument spec**:
The minimal per-symbol metadata the guard and [[Quantization|quantizer]] need — tick, lot/step,
min-notional — from config (paper) or the venue meta endpoint (Hyperliquid). Not a full
instrument provider. See ADR-0017.
_Avoid_: instrument, contract definition (we model only these few fields).

**MarketTick**:
The immutable [[Event]] a [[MarketFeed]] emits carrying a symbol's latest price; the only market
input the [[Strategy]] and [[Paper exchange]] consume in v1.
_Avoid_: quote, bar, candle (v1 has only ticks).

**Component lifecycle**:
The shared contract of every long-lived component (`MarketFeed`, `Strategy`, `Exchange`,
`Engine`): `async start()`/`stop()`, a `state` of `READY → RUNNING → STOPPED` plus `FAULTED`,
and `on_start`/`on_stop` hooks. `DEGRADED` is deferred. See ADR-0014.
_Avoid_: service lifecycle, daemon (those imply separate processes — see [[Engine]]).

**Invariant violation** vs **handler error**:
The two error classes. A **handler error** (a `Strategy`/`Feed` handler raising on one event)
is logged with the correlation id and the component continues. An **invariant violation** (an
illegal saga transition, a failed checkpoint) is **fail-fast → `FAULTED` → crash-only**
restart. See ADR-0014.
_Avoid_: exception, crash (be specific about which class).

**Correlation id**:
A `ContextVar`-bound id auto-injected into every log line, in two scopes: a **run id** (per
process) and a **per-operation id** — the [[Client order id|cloid]] in a saga, the [[signal_id]]
in signal handling, a reconcile-cycle id during [[Reconciliation]]. See ADR-0020.
_Avoid_: request id, trace id (fine informally), session id.

**Named lifecycle event**:
A stable, documented telemetry name emitted as a structured record (`order.placed`,
`reconcile.frozen`, `ghost.reconciled`, …) — distinct from free-text logs. The catalog is a
**test-assertable contract**: a state-affecting path with no named event is a defect.
Observability is a first-class priority of this system. See ADR-0020.
_Avoid_: log message, log line (a named event is structured and asserted-on).

## Relationships

- The **Engine** hosts one **EventBus**; swapping the bus backend (InMemory ↔ Kafka) changes
  durability and inspectability, never the number of processes.

## Flagged ambiguities

- "worker" in the author's prior system meant a separate OS process per pipeline stage. Here
  the whole pipeline is one process; avoid "worker" for Tickwright components — use the
  component name (feed/strategy/exchange) or **Engine** for the host.
