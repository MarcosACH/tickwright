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
mutated after dispatch (replay/audit depend on stability). Modelled as a `frozen` dataclass with a
small envelope — a deterministic `event_id` (the dedup key), `ts_event`/`ts_init` (UTC epoch ns),
and a `partition_key` property (the bus's ordering key; v1 returns `symbol`). Serialization is a
boundary concern of the [[EventBus]] backend, not the domain type. See ADR-0025.
_Avoid_: message, record (reserve "message" for the transport-level envelope).

**EventBus**:
The transport every component couples through — publish/subscribe by event type. A swappable
*backend* (`InMemoryBus` = in-loop dispatch, `KafkaBus` = same topology over Kafka topics for
durability/replay), **not** a process-topology switch. Delivery is **at-least-once**
(duplicates legal, [[Idempotent consumer|consumers must be idempotent]]) and ordered
**per symbol only** (every event keyed by symbol; cross-symbol order is not guaranteed). The
interface is **pub/sub only** (`publish`/`subscribe`); query-shaped reads (e.g. reconciliation
reading the exchange) are direct Protocol method calls, not bus messages. Dispatch on the
`InMemoryBus` is **synchronous and inline**, with a drain-to-quiescence FIFO for reentrant
publishes (so a cascade mirrors `KafkaBus`'s poll-loop); [[Conflation]] of market data happens
upstream at the feed, never in the bus. On the Kafka path all events ride **one topic keyed by
`partition_key`** so a symbol's whole causal chain stays on one partition. See
ADR-0001, ADR-0002, ADR-0003, ADR-0004, ADR-0023, ADR-0028.
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

**cancel_requested marker**:
A checkpointed boolean+timestamp the [[ExecutionManager]] sets on a `LIVE` [[Order saga]] when it
sends a cancel — **not** a `CANCELLING` state (the order stays `LIVE` and can still fill). It lets
[[Reconciliation]] resolve a vanished order correctly: fills → `FILLED`; no fills + marker →
`CANCELLED`; no fills + no marker → `REJECTED` ([[Ghost]]). See ADR-0026.
_Avoid_: CANCELLING state, pending-cancel (there is no such saga state).

**DENIED** vs **REJECTED** vs **FAILED**:
The three negative terminals, split on "was it sent, and who decided?". `DENIED` = our
pre-trade guard refused it, **never sent** (safe to recreate). `REJECTED` = **sent**, the
**venue** refused it (includes ghost-reconciled). `FAILED` = **sent (or attempted)**, we proved it **never
landed** (recovery resolves a never-landed `PENDING`/`SUBMITTED` here, never a blind resend). Never collapse them — their recovery differs. See ADR-0010.
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
[[Connectivity guard]]). Impls: [[Paper exchange|PaperExchange]], `HyperliquidExchange`; the seam
**accepts N** — each real venue is a self-contained [[Venue adapter]] (two ship only to prove the
seam). See ADR-0011, ADR-0015, ADR-0031.
_Avoid_: broker, venue client, gateway (fine informally).

**Venue adapter**:
The self-contained per-venue module that packages a venue's [[MarketFeed]] + [[Exchange]] +
[[Instrument spec]] sourcing + its `*Config`. Tickwright runs **one venue per process** — scaling to
N exchanges is **N processes**, one per venue, never one engine routing across venues — so venue is a
deployment fact and instrument identity stays symbol-scoped (see [[signal_id]]). Adding an exchange
is an additive adapter module plus a process, never a core change. See ADR-0031, ADR-0032.
_Avoid_: plugin, connector, driver (fine informally); do not imply a runtime registry — wiring is an
explicit [[Composition root]].

**ExecutionManager**:
The single engine-internal orchestrator (not a Protocol) that owns the [[Order saga]]: it
subscribes to [[Signal]]s and [[ExecutionReport]]s, assigns the [[Client order id|cloid]],
checkpoints, drives the FSM, and publishes canonical `OrderEvent`s. The saga is written once and
serves every [[Exchange]]. See ADR-0015.
_Avoid_: execution engine (fine informally), order manager, router.

**ExecutionReport** vs **OrderEvent**:
The two event layers. An **ExecutionReport** is a *raw venue fact* the [[Exchange]] emits
("venue acked/filled/rejected cloid X"). An **OrderEvent** is the *canonical saga transition*
the [[ExecutionManager]] publishes after applying that fact. Both are **typed variant classes**:
`ExecutionReport` splits into `OrderStatusReport` + `FillReport`; `OrderEvent` is one class per
saga transition (`OrderPlaced`/`OrderSubmitted`/`OrderLive`/`OrderPartiallyFilled`/`OrderFilled`/
`OrderDenied`/`OrderRejected`/`OrderFailed`/`OrderCancelled`). See ADR-0015, ADR-0025.
_Avoid_: using them interchangeably — one is venue truth, one is engine state.

**Signal**:
An [[Event]] a [[Strategy]] emits expressing an order intent. A typed pair: **`PlaceSignal`**
(side, qty, price, MARKET/LIMIT, GTC/IOC, `post_only`) and **`CancelSignal`** (its own seq'd
[[signal_id]] plus a `target_signal_id` naming the order to cancel — the strategy references the
`signal_id` it emitted, and the engine re-derives the [[Client order id|cloid]]). Carries a
deterministic [[signal_id]] so replays converge; the [[ExecutionManager]] consumes signals and
turns each into an order saga. See ADR-0026.
_Avoid_: order request, command (the bus has no command pattern — a signal is just an event).

**signal_id**:
The deterministic identity of a [[Signal]], `{strategy_id}:{symbol}:{seq}` with `seq` a
strategy-owned monotonic counter resumed on restart from the saga-store high-water mark —
never the snapshot (ADR-0016). The engine's saga and dedup are keyed on it. **Must be a
pure function of strategy state — never random.** The `SignalId` value object (`domain/ids.py`)
is the single owner of this format: `Signal.signal_id` composes it (`render`) and seq
high-water recovery reads it back (`parse`), so the wire form and the recovery read can never
drift. See ADR-0006.
_Avoid_: signal uuid, request id.

**SignalEmitter**:
The strategy-author's helper (`strategies/emitter.py`) that owns a [[Strategy]]'s monotonic
`seq` counter and builds/publishes its [[Signal]]s (`place`/`cancel`, clock-stamped, returning
the [[signal_id]]). Concentrates the one piece of strategy mechanics that is a correctness spine
— never reusing a [[signal_id]] across restart — so it is not each author's problem. A strategy
**composes** an emitter (holds one as a field); it is never a base class, because the [[Strategy]]
seam is satisfied by shape, not inheritance (ADR-0032). The engine's `set_next_seq()` (ADR-0016)
sets the counter through it.
_Avoid_: strategy base class, signal factory.

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

**Recent-order protection window**:
The second clause of ADR-0011 invariant 3: the slow [[Ghost]] cycle skips ghost evaluation for a
resting order whose last saga event is fresher than the window (default ~30s) — the grace clock
never arms — so a just-acked order the venue's open-orders snapshot has not yet propagated is
never raced onto the ghost path. The fill-history cross-check still runs inside the window, so a
recent order that filled heals immediately. See ADR-0011.
_Avoid_: cooldown, debounce (those undersell the race-the-venue guard).

**Ghost gate**:
The `engine/ghost_gate.py` module that owns ADR-0011 invariant 3 in full: the [[Recent-order
protection window]] pre-filter in front of the grace window, ruling on one absent resting order
with a single verdict — protected, waiting, or [[Ghost|ghost]]. A pure decision object (no bus,
clock, or telemetry) composing the grace-window tracker, so the "is it a ghost yet?" timing rule
reads in one place rather than smeared across the reconciler. See ADR-0011.
_Avoid_: throttle, filter (too generic — this is the specific two-phase ghost-timing rule).

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
quantity/price validity, [[Kill-switch]]. Failure → `DENIED`. Impls: a real guard + `NoopGuard`.
**Not** a RiskEngine (no exposure/position limits — deferred). See ADR-0017, ADR-0026.
_Avoid_: risk engine, risk manager (those imply the deferred portfolio-risk surface).

**Kill-switch**:
A **global, halt-only** flag on the [[PreTradeGuard]]: tripped, every new `PlaceSignal` is `DENIED`
(never sent) while resting `LIVE` orders are left untouched (flatten is a separate, deferred operator
action). Tripped **manually only** (`trip_kill_switch(reason)` wired to `SIGUSR1`, reset to `SIGUSR2`;
automatic
circuit-breakers deferred), and **durable/sticky** — persisted to the [[Store]] and restored on
restart, cleared only by an explicit reset, so a halt outlives a crash. See ADR-0026.
_Avoid_: circuit breaker (implies the deferred automatic-trip policy), panic button (it does not flatten).

**Quantization**:
Rule-based rounding of an order's price and size at the boundary, so the venue never silently
rejects it: size rounds **down** to `sz_decimals` (rounds-to-zero → `DENIED`), price rounds
toward the passive side under the sig-figs ∧ decimals rule (Hyperliquid has no fixed tick).
Uses [[Instrument spec]]s. See ADR-0017.
_Avoid_: rounding (too generic), tick size (Hyperliquid price granularity is
significant-figures-based, not a fixed grid).

**Instrument spec**:
The minimal per-symbol metadata the guard and [[Quantization|quantizer]] need — `sz_decimals`,
`max_decimals`, optional `max_sig_figs`, min-notional — from config (paper) or the venue meta
endpoint (Hyperliquid). **Sourced by the
[[Venue adapter]]**, exposed via the [[Exchange]] Protocol, and wired into the guard by the
[[Engine]] at startup, so the guard stays venue-agnostic. Not a full instrument provider. See
ADR-0017, ADR-0031.
_Avoid_: instrument, contract definition (we model only these few fields).

**MarketTick**:
The immutable [[Event]] a [[MarketFeed]] emits — a **last-trade tick** carrying `price`, `size`,
`aggressor_side`, and the venue `trade_id`, sourced from Hyperliquid's `trades` channel; the only
market input the [[Strategy]] and [[Paper exchange]] consume in v1. Single-price: the
[[Paper exchange]] fills MARKET at the latest price and a LIMIT when a tick crosses it. Prices and
sizes are `Decimal`, never `float`. See ADR-0027, ADR-0029.
_Avoid_: quote, bar, candle (v1 has only ticks — a `MarketTick` is a trade tick, not a quote; bars
are a strategy-internal aggregation, not an engine type — ADR-0027).

**Conflation**:
Shedding stale market data under backpressure by keeping only the **latest tick per symbol**. The
live [[MarketFeed]] conflates at ingress (emitting a `feed.lagged` named event on each drop), never
the [[EventBus]]; it is **market-data only** — [[Signal]]s and order-lifecycle [[Event]]s are never
dropped — and happens **upstream of publish**, so both bus backends see the same stream. The
file-backed replay feed never conflates (replay must stay faithful). See ADR-0023.
_Avoid_: throttling, sampling, debounce (conflation is last-value-wins, not rate-limiting).

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

**Composition root**:
The single place that wires the engine: `build_engine(config)` reads the typed `*Config` objects and
constructs the concrete impls, which the [[Engine]] then receives already-built (the engine never
imports a concrete class). Impl selection is an explicit `match` over a small config discriminant —
adding an impl adds **one arm** at the top of the app, touching no adapter and no engine internal.
**No plugin registry / import-path DSL** (a deliberate non-goal). See ADR-0032, ADR-0021.
_Avoid_: registry, plugin loader, DI container (implies the rejected runtime-pluggability surface).

**Dependency direction** (adapter isolation):
The inward ports-and-adapters rule: a central `domain` (events + seam Protocols + value types)
depends on nothing; concrete impls depend on `domain` only; the [[Engine]] depends on Protocols,
never on a concrete impl; **no adapter imports another adapter and core never imports an adapter.**
Enforced mechanically by an `import-linter` contract in CI — a cross-adapter or core→adapter import
**fails the build**, so decoupling is a gate, not an aspiration. See ADR-0032.
_Avoid_: layering, tidy imports (undersell the enforced boundary).

## Relationships

- The **Engine** hosts one **EventBus**; swapping the bus backend (InMemory ↔ Kafka) changes
  durability and inspectability, never the number of processes.
- The **Engine** hosts exactly one live **Exchange** = **one venue per process**; scaling to N
  exchanges is N processes ([[Venue adapter]]), not one engine routing across venues.
- Concrete impls ([[Venue adapter]]s, bus/store backends, strategies) depend only on the `domain`
  Protocols; the [[Composition root]] is the one place that knows every concrete
  ([[Dependency direction]]).

## Flagged ambiguities

- "worker" in the author's prior system meant a separate OS process per pipeline stage. Here
  the whole pipeline is one process; avoid "worker" for Tickwright components — use the
  component name (feed/strategy/exchange) or **Engine** for the host.
