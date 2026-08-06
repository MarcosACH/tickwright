# Module Map: Tickwright v1 Core Engine

## Source

PRD: [#9 — Tickwright v1 core engine: crash-safe event-driven pipeline (Hyperliquid + paper exchange)](https://github.com/MarcosACH/tickwright/issues/9).
Constraints inherited from ADR-0032 (dependency direction, venue-as-extension-unit, composition root) and ADR-0015 (execution topology). Terms are used exactly as defined in `CONTEXT.md`.

Top-level layout (fixed by this map; the folder tree is this map's artifact per ADR-0032):

```
src/tickwright/
  domain/            # events, Protocols, value types, order FSM — depends on nothing
  engine/
    execution.py     # ExecutionManager
    reconcile.py     # Reconciliation
    cache.py         # Cache
    checkpoint.py    # Checkpointer: the Store's two read-models + their ordered writes
    guard.py         # RealGuard + NoopGuard (+ quantization, kill switch)
    strategy_host.py # StrategyHost: registry, routing, tick gate, snapshots, seq recovery
    runner.py        # Engine: barrier, supervision, containment
  adapters/
    bus/             # InMemoryBus, KafkaBus (+ serde codec at the Kafka edge)
    store/           # SQLiteStore, PostgresStore
    feed/            # ReplayFeed
    paper/           # PaperExchange + FillModel seam
    clock/           # LiveClock, ManualClock
  venues/
    hyperliquid/     # HyperliquidFeed + HyperliquidExchange + spec sourcing + config
  strategies/        # minimal reference Strategy impls
  observability/     # named-event catalog, correlation ids, logging — shared leaf
  app/               # build_engine(config), *Config aggregation, CLI entry
```

## Modules

### domain

**Interface:** The stable contract everything compiles against. Frozen, slotted dataclass events with the ADR-0025 envelope (`event_id`, `ts_event`, `ts_init`, `partition_key` property): `MarketTick`; `PlaceSignal`/`CancelSignal`; `OrderStatusReport`/`FillReport`; the nine class-per-transition `OrderEvent`s. The seam Protocols: `MarketFeed`, `Strategy`, `Exchange` (`fetch_*` returns `None` on failure — the connectivity guard is in the type), `EventBus` (pub/sub only), `Store`, `Clock`, `PreTradeGuard`. Value/identity types: Decimal money, `InstrumentSpec`, `signal_id`/`cloid` derivation, the order-state enum and `Order` saga record with idempotent `apply(event)` (no-op on already-reflected `event_id`; illegal transition raises `InvariantViolation`). Invariants: events are never mutated after dispatch; `event_id` derivations are deterministic and provenance-free; all timestamps UTC epoch ns.

**Responsibilities:** Event taxonomy and idempotency-key derivation; the order-lifecycle FSM (legal transitions, terminal taxonomy `DENIED`/`REJECTED`/`FAILED`, `cancel_requested` marker, applied-`trade_id` tracking, `cum_qty` invariant); id derivation (`signal_id` → `cloid`); the Protocol definitions.

**Seams:** None consumed — this module *defines* the seams others adapt to. Depends on nothing (stdlib only), log-free.

**Depth note:** The deepest module in the repo. Delete it and dedup logic, transition legality, and id derivation scatter into every consumer of every event — the exact failure at-least-once delivery punishes. `Order.apply()` concentrates the crash-safety correctness argument into one unit-testable place with zero infrastructure.

---

### ExecutionManager (`engine/execution.py`)

**Interface:** Engine-internal orchestrator, deliberately **not** a Protocol (ADR-0015). Constructed with the `EventBus`, `Exchange` and `PreTradeGuard` Protocols plus the engine-internal **`Checkpointer`** — which since [#213](https://github.com/MarcosACH/tickwright/issues/213) replaces the separate `Store`, `Cache` and `Clock` arguments, carrying all three so they cannot be pointed at different stores or timelines (see below). Subscribes to `Signal` and `ExecutionReport`; publishes canonical `OrderEvent`s. Callers (the runner) must know: `PENDING` is checkpointed **before** any network send; a timeout never transitions a saga (only reconciliation moves a stuck `SUBMITTED`); guard failure yields `DENIED` without a send; a cancel sets the `cancel_requested` marker on a still-`LIVE` saga. Checkpoint failure raises `InvariantViolation` (fail-fast).

**Responsibilities:** cloid assignment from `signal_id`; pre-trade guard invocation; write-ahead intent + checkpointing to `Store`; driving `Order.apply()` and publishing the resulting `OrderEvent`; translating `CancelSignal` (re-derive cloid from `target_signal_id`, send cancel, set marker); publishing reconciliation's synthetic events (`reconciliation`-flagged).

**Seams:** Consumes the `Exchange`, `EventBus` and `PreTradeGuard` seams directly, and the `Store`/`Clock` seams through the `Checkpointer` — all with two shipped adapters.

**Depth note:** The saga is written once and serves Paper and Hyperliquid identically. Deleting it forks the hardest logic (checkpoints, FSM driving, cloid authority) per venue — the rejected alternative of ADR-0015.

---

### Checkpointer (`engine/checkpoint.py`)

**Interface:** Engine-internal, not a Protocol. Constructed with the `Store`, `Clock` and the venue's `AccountSpec`; **builds** the order `Cache` and the `PortfolioProjection` from that one store and lends them read-only (`.cache`, `.portfolio`, `.clock`). Three write verbs, each owning an ordering a caller could otherwise invert: `checkpoint_fill` (fold → one `checkpoint_ledger` transaction → project both read-models), `checkpoint` (the narrow non-fill write, store-first), and `recover` (the ledger **before** the order cache — ADR-0043 §6/§10). Callers must know: only the `Store` seam's own `InvariantViolation` is relabelled, so a contract break *below* the seam is never reported as a failed write; and a refused fill write leaves both in-memory aggregates ahead of the store, which the raise — not the ordering — is what makes safe (ADR-0014).

**Responsibilities:** owning the one `Store` both read-models project and the one `Clock` that stamps them; sequencing the fill's three-step write and the non-fill's two-step one; the recovery order.

**Seams:** Consumes `Store` and `Clock`. Implements none — there is one of it by decision, as with the `Cache` it holds.

**Depth note:** Deleting it does not move the complexity, it scatters it: the "these all wrap one store" rule returns to the `Engine` as a comment and to every hand-wiring site as a convention, which is exactly the state [#213](https://github.com/MarcosACH/tickwright/issues/213) found — three `ExecutionManager` parameters that could be pointed at three stores, turning ADR-0043 §4's one transaction into a silent split with no failing assertion anywhere. Enforcing it in a constructor is type-level, which ADR-0047 leaves open where a runtime validator would be wrong.

---

### Reconciliation (`engine/reconcile.py`)

**Interface:** Constructed with the `Exchange`, `Cache`, `Clock`, `EventBus`, and a `ReconcileConfig` whose construction enforces the timing rule. Two entry points callers must know: the **startup barrier** (awaited by the runner; wraps the mass-rebuild pass — which returns success/failure — in bounded exponential-backoff retry up to `startup_reconciliation_timeout`, then raises `StartupReconciliationTimeout` → `FAULTED`) and the **continuous cycles** (`reconcile_inflight` on the fast cadence, `reconcile_open_orders` on the slower open-order/ghost cadence; the runner owns the scheduling loop — both run as `engine/cadence.py::run_cadence` tasks in the runner's `TaskGroup`, paced by their `ReconcileConfig` intervals off `Clock.sleep_until`, the virtual-time waiter of ADR-0033, and are cancelled in the reverse shutdown right after the feed stops, one step ahead of the `Exchange` they read). Invariants: a failed venue read (a `VenueReadFailure`, never a view) **freezes** and removes nothing (`[]` never does) — a failed *send* stops the whole pass, an unreadable *body* skips only its own order against a per-cloid wall-clock span (`unreadable_grace_seconds`) that faults on `VenueReadUnresolvable` once the condition is proven durable (ADR-0049); a ghost resolves only after the grace window **and** a fill-history cross-check; a just-acked order still inside the **recent-order protection window** is skipped by ghost evaluation entirely (no grace-arming), though its fill cross-check still runs; every heal is a deterministic, `reconciliation`-flagged synthetic event routed through the `ExecutionManager`; both the retry-budget and the recent-order protection window sit under ghost-grace (ADR-0008/0011 timing rule).

**Responsibilities:** Comparing local non-terminal sagas (by cloid) against venue truth; healing missed fills; resolving ghosts to `REJECTED`/`FILLED`; resolving vanished orders via the `cancel_requested` marker; emitting `reconcile.*`/`inflight.*`/`ghost.*` named events.

**Seams:** Consumes `Exchange.fetch_*` (query-shaped direct calls, never bus messages — ADR-0004). Internally, one `_drive` skeleton runs every cadence (a state filter, a read, a per-order handler), so the connectivity-guard freeze (ADR-0011 inv 1) lives in exactly one place; the continuous-absence bookkeeping lives in `engine/absence.py` as two trackers — `ConsecutiveMisses` (counted in observations: the in-flight retry budget, whose unit is right because the thing budgeted *is* the polling) and `GraceWindow` (measured in wall-clock time, serving both the ghost grace window and ADR-0049's per-cloid unreadable-body span, since both ask whether waiting has been tried) — each owning its reset-on-presence discipline. What is "absent" is the caller's to name: a venue record for the ghost cycle, a readable body for the unreadable span. The open-order cadence reads its ghost decision from `engine/ghost_gate.py`, where a `GhostGate` composes the `GraceWindow` behind the recent-order protection pre-filter — ADR-0011 inv 3 in full, protect-while-fresh then measure-continuous-absence, resolved to one `GhostVerdict` in one place.

**Depth note:** The correctness net under at-least-once delivery and crash recovery. Delete it and every consumer must individually distinguish outage from emptiness and duplicate from heal — the freeze/ghost/cross-check policy concentrates here.

---

### Cache (`engine/cache.py`)

**Interface:** The in-memory read-model of current order state — a write-through projection of the `Store`, **never** the source of truth. Callers must know: rebuilt from the `Store` on startup (recovery step 2); reads are direct method calls (pull-then-subscribe — strategies pull open-order state at `on_start` because startup events predate their subscription); writes flow only through the `ExecutionManager`'s checkpoint path.

**Responsibilities:** Projecting saga records into "what is true now" queries (open orders per strategy/symbol, saga lookup by cloid, last-event recency per cloid for the ghost cycle's protection window) for strategies, the reconciler, and the manager.

**Seams:** None — one concrete class. Backed by the `Store` seam.

**Depth note:** Passes the deletion test by concentrating the projection: without it, strategies, reconciler, and manager each query the `Store` with their own notion of "current", and recovery's rebuild step has no single owner.

---

### PreTradeGuard (`engine/guard.py`)

**Interface:** The `PreTradeGuard` **Protocol lives in `domain`**; the two adapters live here: `RealGuard` and `NoopGuard`. Callers (the `ExecutionManager`) must know: check → quantize → verdict; failure means `DENIED` — the order is never sent and is safe to recreate. Quantization rules: size rounds **down** to `sz_decimals` (rounds-to-zero → `DENIED`); price rounds toward the passive side under the sig-figs ∧ decimals rule. The kill switch is global, halt-only, durable/sticky (persisted via `Store`, restored before the feed starts), tripped/reset manually (`SIGUSR1`/`SIGUSR2` wired by the runner); tripped ⇒ every new `PlaceSignal` is `DENIED`, resting `LIVE` orders untouched.

**Responsibilities:** Min-notional and quantity/price validity; quantization against `InstrumentSpec` (wired venue-agnostically by the composition root at startup); kill-switch state and persistence.

**Seams:** `PreTradeGuard` — two real adapters (`RealGuard`, `NoopGuard`).

**Depth note:** Deliberately thin (not a RiskEngine — ADR-0017), but still deep relative to its interface: rounding/sig-fig subtleties and halt semantics concentrate here instead of leaking into the manager or each venue adapter.

---

### StrategyHost (`engine/strategy_host.py`)

**Interface:** `StrategyHost(bus, clock, store, tick_staleness_ns=None)` with `register(strategy, *, symbols)`, `start()`, `stop()`. Callers (the runner) must know: registration fails fast on a duplicate `strategy_id` (ADR-0018); `start()` recovers each strategy — restore persisted snapshot (an incompatible one emits `strategy.snapshot_incompatible` and starts fresh, ADR-0016), set `next_seq` from the saga high-water (places **and** cancel intents consume seqs, ADR-0026), then subscribe wrapped handlers; `stop()` takes the final `snapshot()` of every strategy.

**Responsibilities:** The strategy side of ADR-0024's origin-based containment: `Strategy.on_tick`/`on_order_event` run inside the net — catch, emit `strategy.error` correlated by the triggering event's identity, continue; `InvariantViolation` pierces. Per-strategy routing (declared-symbol tick filtering, own-`strategy_id` `OrderEvent` filtering — a wrapper concern, never a bus feature, ADR-0018) and the per-symbol monotonic tick gate + configurable staleness threshold (ADR-0025).

**Seams:** Hosts N `Strategy` instances (per-strategy routing/seq/snapshot); consumes `EventBus`, `Clock`, `Store` Protocols only.

**Depth note:** Everything that makes an untrusted, trivially-written strategy safe to run — idempotent tick delivery, seq-safety across restart, blast-radius containment — concentrates here so it never smears into strategy code or the bus.

---

### Engine runner (`engine/runner.py`)

**Interface:** `Engine` with the ADR-0014 component contract (`async start()/stop()`, `READY → RUNNING → STOPPED` + `FAULTED`) and `run()` for supervised operation. Callers (the `app` entrypoint) must know: startup is the ADR-0024 ordered sequence gated on the **reconciliation barrier** (feed starts last; nothing places before the barrier clears; bounded retry then fail-fast `FAULTED`); shutdown is the reverse, bounded by `shutdown_timeout`, takes final strategy snapshots (`StrategyHost.stop()`), **leaves resting `LIVE` orders alone**, exits 0; any invariant violation → `FAULTED` → non-zero exit. Exit-code contract: 0 = graceful, non-zero = restart-me.

**Responsibilities:** Composing the `StrategyHost` into the lifecycle (it owns the strategy-side containment, routing, and tick gate above); wrapping the remaining third-party surface (feed parse callbacks → `handler.error`) while engine-internal handlers subscribe raw; `asyncio.TaskGroup` supervision; OS signal handling (`SIGINT`/`SIGTERM` graceful, `SIGUSR1`/`SIGUSR2` kill switch); the run-id correlation binding.

**Seams:** Consumes every domain Protocol.

**Depth note:** The ordering rules (barrier before feed, pull-then-subscribe, reverse shutdown) and the containment policy are exactly the knowledge that would otherwise smear across every component; the runner is where "crash and graceful stop converge on one recovery path" is enforced.

---

### bus (`adapters/bus/`)

**Interface:** Two `EventBus` adapters. `InMemoryBus`: synchronous in-loop dispatch with a drain-to-quiescence FIFO for reentrant publishes; no serialization (events pass by reference). `KafkaBus`: same topology over one topic keyed by `partition_key`; `msgspec` serde codec at this edge only. Callers must know the shared contract, not the backend: at-least-once delivery, per-symbol ordering only, pub/sub only.

**Responsibilities:** Dispatch mechanics, Kafka producer/consumer lifecycle, offset commits, wire-format encoding (Kafka side).

**Seams:** `EventBus` — two real adapters. Kafka-internal, none a public seam: the serde codec (wire format), `Subscriptions` (the type-guarded, registration-ordered fan-out both backends share, so parity of delivery is one module not two copies), and `DrainLedger` (the `KafkaBus` produced-vs-committed offset fence, tested at its own interface for the off-by-one conventions the drain rests on).

**Depth note:** "Swapping the backend changes durability, never behavior" lives or dies here; the drain-to-quiescence FIFO mirroring the Kafka poll loop is the concentrated trick that keeps both backends observationally equivalent. The shared `Subscriptions` makes delivery-parity structural (not two copies agreeing), and the `DrainLedger` isolates the offset accounting the Kafka drain fence rests on.

---

### store (`adapters/store/`)

**Interface:** Two `Store` adapters: `SQLiteStore` (default; file or `:memory:`), `PostgresStore`. Holds exactly three things (ADR-0019): saga records keyed by cloid (state, history, timestamps, venue oid, reasons, cancel intent), opaque strategy snapshot bytes per `strategy_id`, kill-switch state. Callers must know: seq high-water-mark is **derived** from saga records (no separate table); there is **no** processed-event-id table; the store is per-process, never shared.

**Responsibilities:** Schema, transactional checkpoint writes, recovery reads.

**Seams:** `Store` — two real adapters. Canonical pairings: InMemoryBus+SQLite, KafkaBus+Postgres.

**Depth note:** Concentrates durability; the checkpoint write the whole crash-safety argument rests on is one tested code path per backend.

---

### feed (`adapters/feed/`)

**Interface:** `ReplayFeed` — the non-venue `MarketFeed` adapter: file-backed, deterministic, **never conflates** (replay must stay faithful). Paired with `ManualClock` for test-time control.

**Responsibilities:** Reading recorded ticks, emitting them as `MarketTick`s on the bus in order.

**Seams:** Adapter of `MarketFeed` (the venue package provides the second adapter).

**Depth note:** Small by design; earns its keep as the hermetic half of the feed seam — the tracer E2E and all strategy tests stand on it.

---

### paper (`adapters/paper/`)

**Interface:** `PaperExchange` — the deterministic in-process `Exchange` adapter and default v1 target. Callers must know: fills MARKET at the latest `MarketTick`, holds a book of resting LIMITs re-checked each tick; it **self-subscribes to the tick stream at construction** (filling off ticks is what a paper venue *is*, a real venue would not), so neither the composition root nor a test wires a tick line; frictionless (price+quantity, no fees/margin/PnL); zero setup, no keys. The **`FillModel` Protocol lives in this package** (paper-internal seam): `ImmediateFillModel` (deterministic, optimistic, zero-slippage, full-fill) and `StochasticFillModel` (seeded queue/slippage/partials/latency); both take injected RNG + `Clock`.

**Responsibilities:** Resting-order book, fill decisions (delegated to the fill model), emitting `ExecutionReport`s, honest `fetch_*` for reconciliation.

**Seams:** Adapter of `Exchange`; defines the `FillModel` seam with two real adapters.

**Depth note:** The engine's whole hermetic test story hangs on this being a *real* exchange (never a mock); the fill-model seam isolates the only nondeterminism-shaped decision so determinism is a wiring choice.

---

### hyperliquid (`venues/hyperliquid/`)

**Interface:** The one self-contained venue package (venue = extension unit, ADR-0031/0032): `HyperliquidFeed` (async WS, two public channels per coin — `trades` → `MarketTick`, `activeAssetCtx` → `MarkTick` from `ctx.markPx` (ADR-0039) — both unauthenticated, so the feed holds no key material at all; conflation at ingress with `feed.lagged`, keyed `(stream, symbol)` so a mark never swallows the trade queued beside it), `HyperliquidExchange` (async HTTP + SDK/`eth-account` signing utilities only; MARKET → aggressive-IOC-limit translation with slippage bound; `post_only` → ALO; perps only), instrument-spec sourcing from the meta endpoint, and `HyperliquidConfig`. Callers must know: signing key is env-only, never persisted, redacted from logs; `fetch_*` → `None` on failure; testnet/mainnet via `TICKWRIGHT_HYPERLIQUID__TESTNET`.

**Responsibilities:** All venue knowledge — symbol/asset mapping, endpoints, auth, quirk translation. No saga, no engine imports, no other-adapter imports.

**Seams:** Adapter of `MarketFeed` + `Exchange`; source of `InstrumentSpec`.

**Depth note:** Adding venue N touches a new package like this one, a `*Config`, one composition-root arm, and deployment — nothing in the core. This package is the proof.

---

### strategies (`strategies/`)

**Interface:** Minimal reference `Strategy` adapters (engine capability, not a library). Authors must know: `on_tick`/`on_order_event`; emit `PlaceSignal`/`CancelSignal` via a composed `SignalEmitter` (`emitter.py`) that owns the strategy-owned monotonic `seq` (resumed at startup via the engine-set `set_next_seq()`, ADR-0016) and clock-stamps + publishes each signal; own state *content* via `snapshot()`/`restore()` (engine persists the bytes); cancel by your own `signal_id`; handler exceptions are contained, not fatal.

**Responsibilities:** Signal logic only. No persistence, no venue knowledge, no saga awareness beyond `OrderEvent`s.

**Seams:** Adapter(s) of `Strategy` — the seam accepts N; shipped impls prove it.

**Depth note:** Shallow on purpose — the point of every other module is that this one stays trivial to write.

---

### clock (`adapters/clock/`)

**Interface:** Two `Clock` adapters: `LiveClock` (wall clock, real async waits), `ManualClock` (explicit virtual-time advance). Invariant: engine code never touches `asyncio.sleep`/`time.time()` directly; canonical time is UTC epoch ns.

**Responsibilities:** Reads, waits, timers.

**Seams:** `Clock` — two real adapters.

**Depth note:** The reason the whole suite never sleeps; deleting it re-scatters time into every timeout, cadence, and staleness check.

---

### observability (`observability/`)

**Interface:** The shared leaf (ADR-0020/0032): the **named-event catalog** — the closed `NamedEvent` enum, stable documented telemetry names, a test-assertable contract — and `named_event(NamedEvent, **fields)`, which refuses any uncataloged name; correlation-id `ContextVar`s via `bind_run_id` (per-process run id) and `operation(**ids)` (per-operation `cloid`/`signal_id`/`cycle`), merged into every record so no call site repeats them as fields; `configure_logging(stream, json_output, secrets)` wiring the structlog chain with redaction of registered secret **values** and sensitive field **names**. A `testing.capture_events()` seam runs the merge+redaction chain so tests assert on what a real line carries. Importable by engine, venues, and the `bus`/`store`/`feed`/`paper` adapters; **never** imported by `domain`, `clock`, or `strategies` (the last two stay domain-only — they emit no named events).

**Responsibilities:** Emitting/asserting named events; ambient correlation binding; log configuration and secret redaction.

**Seams:** None — one concrete implementation.

**Depth note:** "A state-affecting path with no named event is a defect" is only enforceable if the catalog is one importable artifact tests can walk — the `test_catalog_walk` census drives every `NamedEvent`'s real path and fails if a name has no path-and-test.

---

### app (`app/`)

**Interface:** The composition root: `build_engine(config) -> Engine` plus the CLI entry (`asyncio.run(engine.run())`). Takes the pure `AppConfig`, which composes the typed `*Config` objects (each adapter's config lives in its package; only `app` knows them all), and selects impls with an explicit `match` over config discriminants (`exchange: paper|hyperliquid`, `bus: in_memory|kafka`, `store: sqlite|postgres`). No registry, no import-path DSL.

Ambient config is read in exactly one place: `AppSettings`, the `pydantic-settings` skin over `AppConfig` that `__main__` alone builds. `AppConfig` itself is a pure `BaseModel` and reads neither the environment nor `.env` — a config class that reads them cannot also be the class tests build by hand, since both sources outrank a class default and would wire a live venue into a paper test (issue #71).

**Responsibilities:** Constructing every concrete, injecting already-built dependencies into the `Engine`, wiring instrument specs from the venue into the guard.

**Seams:** None of its own — it is the one place that knows every adapter.

**Depth note:** Exactly one module at the top of the graph may know all concretes; adding an impl is one `match` arm here and nowhere else.

## Dependency graph

```
app ────────────▶ engine, adapters/*, venues/*, strategies, domain, observability
engine ─────────▶ domain, observability          (Protocols only — never a concrete impl)
adapters/bus ───▶ domain, observability
adapters/store ─▶ domain, observability
adapters/feed ──▶ domain, observability
adapters/paper ─▶ domain, observability
adapters/clock ─▶ domain
venues/hyperliquid ▶ domain, observability
strategies ─────▶ domain
domain ─────────▶ (nothing)
observability ──▶ (nothing inward; stdlib/logging only)
```

No cycles. Enforced by the `import-linter` contract in CI (ADR-0032): core → adapter and adapter → adapter imports fail the build. `domain` imports nothing and stays log-free; `clock` and `strategies` stay domain-only (they never import `observability`).

## Out of scope

- **`ExecutionManager` / `Reconciliation` / `Cache` as Protocols** — engine-internal, one implementation each; a seam needs two real adapters (ADR-0015).
- **A DataEngine/ExecutionEngine layer** — the `Strategy`/`ExecutionManager` split *is* the data/execution split (ADR-0015).
- **`FillModel` in `domain`** — only `PaperExchange` calls it; it is a paper-internal seam (map decision, 2026-07-02).
- **Seam-first venue packaging** (`feeds/hyperliquid/` + `exchanges/hyperliquid/`) — fragments venue knowledge; rejected in ADR-0032.
- **A plugin registry / entry-point discovery** — rejected in ADR-0032; wiring is the explicit `match` in `app`.
- **A processed-event-id table module** — dedup lives in `Order.apply()` (ADR-0025/0019).
- **A RiskEngine module** — the guard is deliberately thin; portfolio risk is deferred (ADR-0017).
- **A separate `serde` module** — the codec is a `KafkaBus`-edge concern, not a shared seam (ADR-0025).
