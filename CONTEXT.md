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
interface is **pub/sub plus lifecycle** (`publish`/`subscribe`, and `start`/`drain`/`close`
for the runner's ordered startup and reverse shutdown, ADR-0024 — all no-ops in-memory);
never query-shaped: reads (e.g. reconciliation reading the exchange) are direct Protocol
method calls, not bus messages. Dispatch on the
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

**Symbol ownership**:
Which [[Strategy]] declared which symbol, and the rule that **at most one** may declare each:
`(strategy, symbol)` disjoint per [[Account]] — process-wide, one account per process (ADR-0038).
Not a preference but a consequence of `NET` netting: a one-way venue merges two same-symbol
strategies into a single real [[Position]], so their per-strategy books would stay arithmetically
consistent while describing an isolation the venue does not provide. Same-symbol isolation is a
**separate account**, and therefore a separate process. Carried as the `SymbolOwnership` value
(`domain/ownership.py`), which owns the symbol→owner index and the sentence a violation is refused
with; **two gates** read it — `AppConfig` refuses a *configured* overlap at load, `StrategyHost`
a *registered* one — differing only in exception type, since pydantic converts a `ValueError`
where the registry raises `InvariantViolation`. Unconditional in v1 (both adapters `NET`); a
`HEDGE` adapter is the documented extension point that would relax it. See ADR-0034, ADR-0038,
ADR-0018.
_Avoid_: exclusivity (reserved for ADR-0038's one-process-per-account invariant), symbol
allocation, sharding, routing (the `StrategyHost` wrapper's per-strategy tick filtering is a
*delivery* concern and holds whether or not symbols overlap).

**Exchange** *(Protocol)*:
A **thin boundary adapter** translating venue ↔ our types: `place`/`cancel`/`fetch_*` and
emitting raw [[ExecutionReport]]s. Owns no saga. A failed `fetch_*` never answers as truth (the
[[Connectivity guard]]), and the grain decides how: `fetch_order` returns a [[Failed read]] —
never a view — because the reconciler behind it drives a worklist and acts on *which way* the
read failed; `fetch_account_state` reads one grain with nothing behind it to spare and collapses
both to `None`. Impls: [[Paper exchange|PaperExchange]], `HyperliquidExchange`; the seam
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
The system-of-record behind the [[Cache]] and the [[PortfolioProjection]]: holds order saga
records (keyed by [[Client order id|cloid]]), [[Strategy]] snapshots, the kill-switch state, and
the accounting ledger — [[Position]] rows keyed by `(strategy_id, symbol)`, a single [[Account]]
row, and one funding watermark per symbol. Ledger rows are **current-state, upserted in place**
(recovery is a read, never a replay), and a ledger mutation is written in **one transaction with
the order checkpoint** it belongs to. Impls: `SQLiteStore` (default, zero-setup) +
`PostgresStore` (production parity).
Paired with the [[EventBus]] backend — InMemory+SQLite or Kafka+Postgres. See ADR-0019, ADR-0043.
_Avoid_: database, persistence layer (fine informally), repository.

**Cache**:
The in-memory read-model of current order/position state — a **write-through projection** of
the durable order store, not the source of truth. Rebuilt from the store on restart. Answers
"what is true now"; the store answers "how did we get here." See ADR-0009.
_Avoid_: source of truth, database (the durable store is the truth; the cache projects it).

**Checkpointer**:
The engine-internal owner of a [[Store]]'s **two** read-models — the order [[Cache]] and the
[[PortfolioProjection]] — and of the ordered writes that move them. It **builds** both from one
store, so the identity the fill's single transaction rests on (`store is cache's is
portfolio's`, ADR-0043 §4) is a constructor fact rather than a convention a wiring site has to
remember; it holds the run's [[Clock]] for the same reason, so a saga's event stamps and its
durable stamps share one timeline. Three verbs, each a rule a caller could otherwise invert:
the atomic fill write, the narrow non-fill checkpoint, and [[Recovery]]'s ledger-before-order-cache
order. It lends the two projections out for **reads** — the [[Reconciliation|reconciler]]'s
worklist, the [[ExecutionManager]]'s saga lookups, the scoped [[Portfolio]] facade — and owns
only what writes them. Introduced by issue #213.
_Avoid_: unit of work, transaction manager (it sequences writes, it does not open transactions —
the [[Store]] owns those), repository, read-model registry.

**Recovery** (snapshot-plus-reconcile):
On restart: restore the [[Cache]] from the durable store, [[Reconciliation|reconcile]] each
non-terminal order by [[Client order id|cloid]] against the venue, resume sagas. Event replay
is **not** the recovery path — only idempotent event *application* converges. See ADR-0009.
_Avoid_: event replay, event sourcing (those are a deferred audit capability, not recovery).

**Reconciliation**:
The healer that periodically compares local order state against the venue's truth and applies
the difference, in two phases (startup mass-rebuild, continuous loops) and two cadences (fast
in-flight check, slower open-order/ghost reconcile). The correctness net under at-least-once
delivery and crash recovery. Anchored on the **cloid**; its economic sibling is
[[Ledger reconciliation]], a separate cycle on a separate anchor. See ADR-0011.
_Avoid_: sync, refresh, polling (those undersell the heal-against-truth role).

**Ledger reconciliation**:
[[Reconciliation]]'s economic sibling — the [[PortfolioProjection]]'s own healing loop, anchored on
the venue's **account/position snapshot** rather than on a cloid, and **live-only** (paper has no
venue to heal from). Splits by tier: **Tier-1** divergence (the accumulated ledger) heals through a
[[Synthetic event]] on the same idempotent apply path *and* alerts; **Tier-2** divergence (recomputed
valuations) only ever alerts, inside a band scaled by the notional the quantity's mark-sensitivity
flows through (`VALUATION_DIVERGENCE`). Per-strategy attribution is **never** reconciled — the venue
has no per-strategy truth — so the residual lands in the unattributed partition and Σ holds by
construction. The [[Connectivity guard]] applies unchanged, plus one of its own: the
[[Account abstraction mode]] is re-verified before any cash heal, and a changed *or unverifiable*
mode **freezes** the account-grain cycle (`ACCOUNT_MODE_UNVERIFIED`) rather than faulting. See
ADR-0034, ADR-0040, ADR-0046, ADR-0044.
_Avoid_: portfolio sync, PnL refresh; **[[Reconciliation]]** unqualified (that one is the order
saga's, on a different anchor with a different freeze grain).

**Connectivity guard** (never-`[]`):
The invariant that a failed venue read returns a [[Failed read]] — never a view, never `[]`; on
one, [[Reconciliation]] **freezes** and removes nothing. An outage must never be misread as "all
orders vanished." The value reads as *no truth to compare against*, which an outage is one route
to: on the account pull [[Ledger reconciliation]] anchors on, the paper exchange answers `None`
**permanently and without failing**, holding no account state to report — same freeze, different
route, and the only value that stays fail-closed if a paper ledger cadence is ever wired by
mistake. Bounded by [[Permanent refusal]], which is the one venue answer this must *not* carry.
See ADR-0011, ADR-0048, ADR-0049.
_Avoid_: empty result, no orders (the whole point is that these differ from a failure).

**Failed read** (`VenueReadFailure`):
The two-member vocabulary the [[Connectivity guard]] answers with, and the whole of what a
caller needs to decide **how much** one order's failure costs. `SEND_FAILED`: no body arrived,
so the venue may be unreachable — the pass stops, since every order behind this one would pay a
30s request timeout to learn the same thing. `UNREADABLE_BODY`: a body arrived and could not be
read, from a venue that is up and answering at full speed — that order alone is skipped, its
per-cloid span runs, and the pass carries on. Both are still "not venue truth"; neither is ever
a view. The span is what makes the durable case escape the transient verdict: staying unreadable
across `unreadable_grace_seconds` of waiting is the more-than-one-sample a [[Permanent refusal]]
cannot be told from otherwise, and spending it raises `VenueReadUnresolvable` — never a terminal
state invented for an order whose body was never read. Measured in **wall-clock and never in
reads**: the three drivers poll 5s, 30s and the startup barrier's backoff apart, so a count
would buy a different amount of waiting under each. See ADR-0049 §4.1, ADR-0048.
_Avoid_: `None` read, failed request (the first names the old single sentinel, the second is
what an operator sees on either member); unreadable-read *budget* (says count where the unit is
time).

**Permanent refusal**:
A venue answer that is understood and cannot be represented — a fill fee settled in a token
other than USDC, against money that is a bare `Decimal` with USDC implicit (ADR-0029). Named
against the two *transient* failures beside it, a dead transport and an unreadable body, which
the [[Connectivity guard]] answers with a [[Failed read]] and a retry at the next deadline. The
venue has already stored the fact, so a retry re-reads it identically forever: answered as
transient it would freeze the reconcile cycle permanently and silently. It refuses as
`VenueFactUnsupported`, deliberately outside the `UNREADABLE` vocabulary every transient guard
catches, and **faults** the engine — the one condition an operator, not a deadline, has to
resolve. Its sibling `VenueReadUnresolvable` faults for the same reason with less knowledge:
nothing was ever read there, and the permanence is *inferred* from a spent budget rather than
observed in one sample ([[Failed read]]). See ADR-0048, ADR-0049, ADR-0036.
_Avoid_: parse error, bad response (both read as the transient case this exists to be distinct
from); hard failure (says how loud, not why it cannot be retried).

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
tick. It also stamps each fill's [[Fee]], computed after matching from the instrument's rates —
so the [[Fill model]] still emits price + quantity only. Margin and PnL stay deferred.
See ADR-0012, ADR-0036, ADR-0013.
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

**Figure**:
One numeric value as an outside source *reports* it — a venue response body, a replay row —
before it is a domain quantity. A figure is unreadable, and therefore a failed read at whatever
grain read it, unless it is an exact number: `Decimal("nan")`/`Decimal("Infinity")` construct
cleanly, and a figure re-typed as a JSON *number* has already lost digits and scale to `float`
in `json.loads` before any parse of ours sees it, so neither is caught by the
absence of an exception. The universal half of the guard is `domain.exact_figure`; what a
figure may be *encoded* as is each venue's own contract (Hyperliquid: a decimal string, in
`venues/hyperliquid/reading.py`). See ADR-0029, ADR-0011 inv 1.
_Avoid_: value, amount, number (all read as "the quantity we hold" rather than "what the venue
said"). A figure is the *reported* form; the `Decimal` it becomes is a domain quantity.

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

**MarkTick**:
The immutable [[Event]] a [[MarketFeed]] emits carrying only a symbol's **mark price** — defined
here, having no entry of its own: the venue's robust-median valuation price, distinct from the
last-trade [[MarketTick]] price. It is the input the
[[PortfolioProjection]] recomputes Tier-2 valuations from (unrealized PnL, equity, margin, effective
leverage, liquidation price); **never a fill input** and **never seen by a [[Strategy]]** in v1
(reached only through the [[Portfolio]] seam). Provenance differs by deployment, compute does not:
live carries the venue mark (`activeAssetCtx`), paper and replay carry the **last-trade proxy** (the
latest [[MarketTick]] `price`), so there is one mark per deployment and no runtime fallback.
[[Conflation|Conflates]] last-value-wins per symbol like a [[MarketTick]] — on its own stream,
never against one; a stale mark **freezes**
at its last value, a wholly-absent mark makes the mark-dependent Tier-2 reads **`None`** (never a
fabricated flat). Prices are `Decimal`. See ADR-0039, ADR-0034, ADR-0027.
_Avoid_: oracle price (a different venue price — used for funding, not margining), mid, index price.

**Conflation**:
Shedding stale market data under backpressure by keeping only the **latest value per stream per
symbol** — [[MarketTick]] and [[MarkTick]] are two streams, each last-value-wins **on its own**,
because a mark is not a later version of a trade and swallowing one for the other would stop the
account filling against a stream that is still arriving. The
live [[MarketFeed]] conflates at ingress (emitting a `feed.lagged` named event on each drop), never
the [[EventBus]]; it is **market-data only** — [[Signal]]s and order-lifecycle [[Event]]s are never
dropped — and happens **upstream of publish**, so both bus backends see the same stream. The
file-backed replay feed never conflates (replay must stay faithful). See ADR-0023.
_Avoid_: throttling, sampling, debounce (conflation is last-value-wins, not rate-limiting).

**Component lifecycle**:
The shared contract of every long-lived component (`MarketFeed`, `Strategy`, `Exchange`,
`Engine`): `async start()`/`stop()`, a `state` of `READY → RUNNING → STOPPED` plus `FAULTED`,
and `on_start`/`on_stop` hooks. `DEGRADED` is deferred. That pair is the shared *minimum*, not
the whole of any one lifecycle: a component running a loop of its own also has a **supervised
long-lived half** the engine's `TaskGroup` holds, so a failure in it faults the run instead of
killing a task nobody watches. `MarketFeed`'s is `start()` itself, which the runner never awaits
inline; `Exchange` declares a third verb, `run()`, because ADR-0024 step 4 *does* await
`Exchange.start()` inline and it must return for the barrier to run. See ADR-0014, ADR-0024.
_Avoid_: service lifecycle, daemon (those imply separate processes — see [[Engine]]).

**Invariant violation** vs **handler error**:
The two error classes. A **handler error** (a `Strategy`/`Feed` handler raising on one event)
is logged with the correlation id and the component continues. An **invariant violation** (an
illegal saga transition, a failed checkpoint) is **fail-fast → `FAULTED` → crash-only**
restart. See ADR-0014.
_Avoid_: exception, crash (be specific about which class).

**Write verb** vs **restore constructor**:
The two ways an aggregate's state moves, and the reason [[Event]]s and value objects in `domain/`
carry **no `__post_init__`**. A **write verb** (`Order.apply`, `Position.apply`,
`Account.accrue_realized`) advances state from a fact in flight and is **where a precondition is
stated** — it raises an [[Invariant violation]] rather than admitting a value that would corrupt the
operation. Each rule attaches to the verb that cannot proceed without it, so a verb whose
preconditions are all owned upstream states none: `Account.accrue_realized` writes the cash line
**unconditionally**, because `Position.apply` already owns the key it would have deduplicated on.
A **restore constructor** (`Order.restore`, `Account.restore`,
`restore_position`) rebuilds persisted state
field-by-field and **deliberately bypasses those checks**, because a checkpointed row is the outcome
of transitions already adjudicated — re-adjudicating it would refuse to recover a correct saga.
`apply` guards the future; `restore` reproduces the past. Constructor-level validators are reserved
for **configuration chosen at wiring** (`StochasticParams`, `ReconcileConfig`), validated at its
declaration site in the config layer because there is no later operation to attach a rule to —
never on the domain dataclass a configured value is deserialized into ([[AccountSpec]],
[[Instrument spec]]): a knob's rule belongs to the config model that declares it
(`PaperExchangeConfig`, `AppConfig`), and a **venue-sourced** spec is a *report* of venue truth, a
fact in flight like any other. See ADR-0047, ADR-0014, ADR-0008, ADR-0043.
_Avoid_: validation, sanitization (name the verb that is guarded).

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

**Position**:
The per-`(account, strategy, symbol)` economic aggregate of one instrument's net exposure —
signed size, average entry, realized PnL, accrued fees and funding, cash impact (the exact
Tier-1 "ledger" it accumulates through an idempotent `apply`) — with unrealized PnL and notional
**recomputed from a mark**, never stored. The `account` is ambient (one per process, ADR-0038) and
`strategy` is nullable: **`None` is the reserved unattributed partition** holding flow the engine
never placed. A pure `domain` aggregate, the economic sibling of the [[Order saga]]. See ADR-0035,
ADR-0034, ADR-0038.
_Avoid_: holding, lot, order (the FSM saga is the [[Order saga]]).

**Account net size**:
How much of one symbol the [[Account]] holds, **signed** — `Σ(per-strategy signed size)` over
**every** [[Position]] partition of that symbol, the reserved unattributed one included. The
left-hand side of ADR-0034's bridging invariant `Σ(per-strategy signed size) = account net size =
venue szi`, and the one answer to "how much is held": a symbol traded to flat reports a **zero**
rather than dropping out, so "held nothing" stays distinguishable from "never traded". Linear and
summable, unlike PnL — which is why *this* is what reconciles against the venue and per-strategy
PnL never does. Computed by `domain.account_net_size` over whichever partitions the caller has
(the durable mass-read on recovery, the projection's map in flight). The [[Paper exchange]] reads
it for its funding notional because it holds no position state of its own; a live venue is asked
nothing, because a real one knows. See ADR-0034, ADR-0037, ADR-0043 §4.
_Avoid_: net exposure (the deferred RiskEngine's signed portfolio quantity — [[Notional]]),
position size unqualified (a *partition's* size is one term of this sum), venue size (the
right-hand side, which is the venue's own report and only equal by invariant).

**Account**:
One collateral pool's balances — `total = locked + free` — plus its **reported** margin, effective
leverage, and liquidation price recomputed from marks; the boundary reconciled against the venue's
account snapshot (the sole reconciliation anchor, ADR-0034). A **deployment fact**: one [[Engine]]
process trades exactly one, owned exclusively (ADR-0038). See ADR-0035.
_Avoid_: wallet, balance (a facet); not `eth_account.Account` (the signing library's unrelated type).

**Account abstraction mode**:
The venue-side setting that decides whether an [[Account]]'s spot and perps balances are separate
or pooled — and therefore **what the venue's perps account snapshot means**. Tickwright supports
**Manual/Standard only** (`userAbstraction` reading `default` or `disabled`), where the perps
clearinghouse *is* the account boundary; under `unifiedAccount` or `portfolioMargin` the same
snapshot reports only the collateral posted into perps, so equity and free margin read an order of
magnitude low. Not configuration — it is **read from the venue** and verified at boot, and again
before any Tier-1 cash heal. The guard **fails closed at both points**: at boot an unsupported *or
unreadable* mode refuses to start (`VenueAccountModeUnsupported`); in flight a mode that changed or
could not be verified refuses the heal, freezes the account-grain reconcile and alerts
(`ACCOUNT_MODE_UNVERIFIED`) — an unverified mode is never read as an unchanged one. A
[[Venue adapter]] concern that never reaches `domain`. See ADR-0046, ADR-0038, ADR-0034.
_Avoid_: margin mode (that is per-symbol cross/isolated — see [[Leverage]] & Margin mode), account
type, unified margin.

**AccountSpec**:
The venue adapter's **static declarations** about the [[Account]] it trades — the qualified
`account_id` (venue + network + venue-native identifier) and the `NET`/`HEDGE` netting semantics —
exposed on the [[Venue adapter]]'s `Exchange` seam beside the instrument specs and read once at
composition. `AccountSpec` is to [[Account]] as the instrument spec is to the instrument: static
declaration, never live balances. Carries **no collateral currency** in v1 (USDC is implicit,
ADR-0042 §2); on the paper venue the `account_id`'s label half comes from `PaperExchangeConfig`
(lowercase slug, no hyphen, so `paper-<label>` stays unambiguously two segments against live's
three). See ADR-0038, ADR-0042, ADR-0031.
_Avoid_: account config (it is adapter-authored, not operator-authored), account state/snapshot
(that is the venue's live truth the [[PortfolioProjection]] reconciles against).

**Portfolio** *(seam)*:
The pull-style read seam a [[Strategy]] queries for [[Position]]/[[Account]] state — reads are
**synchronous method calls, never a PnL subscription** (ADR-0004). Three methods: `position(symbol)`
and `open_positions()` (frozen [[PositionView]]s — this strategy's **own** attribution beside the
whole-position economics), and `account()` (the account-wide shared pool, a frozen [[AccountView]]).
**Scoped to the strategy at injection** (a per-`strategy_id` facade, like the `SignalEmitter`) and
**constructor-injected by the [[Composition root]]** — no `strategy_id`/`venue`/`account_id`
argument, no change to the [[Strategy]] Protocol. The unattributed partition (`strategy_id=None`) is
**off the seam** (engine/telemetry-only). Delivers ADR-0017's deferred positions-tracker; **not**
the portfolio-*risk* surface. See ADR-0041, ADR-0035.
_Avoid_: portfolio risk, RiskEngine (enforcement, deferred), position manager.

**PositionView** & **AccountView**:
The **frozen `domain` value snapshots** the [[Portfolio]] seam returns — read-only copies computed at
read time, **distinct from** the mutable [[Position]]/[[Account]] aggregates (which carry `apply()`
and move under the reader). Each is **internally coherent by construction** (all fields from one
`(position, mark)` read). `PositionView` carries **two grains** — the strategy's **own-attribution
slice** (size, entry, realized/unrealized PnL, fees, funding) and the symbol's **whole-position
economics** off the account-net `szi` (notional, leverage, margin mode, margin used, maintenance,
liquidation price, effective leverage, plus `mark_ts`), which coincide in v1 except under foreign
flow; `AccountView` carries the shared pool (equity, cash, total margin/maintenance, free margin,
effective leverage). The raw mark **value** is not exposed — only its freshness `mark_ts` (ADR-0039:
the mark is an accounting input, not a strategy signal). **Tier-1 fields are never `None`;
mark-dependent Tier-2 fields (and every account Σ with a mark-dependent term) are `Decimal | None`**
— `None` only when the mark is absent **and that field's own terms need it**, so a flat position's
valuations read `0` rather than `None` (a stale mark freezes; the strategy judges staleness from
`mark_ts`, ADR-0039); effective leverage is additionally `None` on a non-positive denominator.
`position()` returns `None` only for a never-traded symbol; a **flat-with-history** record reads
`size=0` with realized retained, its position-grain valuations degenerating (no liquidation price).
See ADR-0041.
_Avoid_: PositionSnapshot/AccountSnapshot (overloads `Strategy.snapshot()`), live view (they are
frozen), DTO.

**PortfolioProjection**:
The write-through projection of [[Position]]/[[Account]] state implementing [[Portfolio]] —
the accounting sibling of the [[Cache]]. Its **Tier-1** ledger is applied **synchronously on the
fill-apply path** (not a fill-bus subscriber); its **Tier-2** mark is fed by **subscribing to
[[MarkTick]]** into a private latest-value map (a non-accumulated cache, ADR-0039). Reconciled
against venue truth on live, rebuilt from the [[Store]] on restart. See ADR-0035, ADR-0034, ADR-0039.
_Avoid_: cache (that's the order read-model), ledger (reserved), portfolio tracker.

**Realized PnL** & **Unrealized PnL**:
The two halves of a [[Position]]'s trade profit — **realized** is booked when a fill reduces or
closes exposure (`signed_closed_size × (exit − entry)` — signed with the closed exposure, so a short
closed below its entry books a profit; accumulated as Tier-1); **unrealized** is the open
exposure marked to a price (`signed_size × (mark − entry)`, Tier-2, recomputed each read from the
[[MarkTick]] mark). Both are **gross**: [[Fee|fees]] and [[Funding]] accrue on their own ledger
lines and are **never** folded in — a venue reporting them bundled is un-bundled at its
[[Venue adapter]]'s `Exchange` seam, so `domain` never carries a venue's convention. Realized is
retained on a flat record; unrealized reads `0` there, needing no mark. See ADR-0045, ADR-0036,
ADR-0037, ADR-0040.
_Avoid_: PnL unqualified (always say which), **Total PnL** (realized + unrealized — deliberately
not a term: no field carries it), net PnL (implies fees deducted; they are not).

**Fee**:
The per-fill trading cost a [[Position]] accrues — a signed `Decimal` (negative = a **maker
rebate**), settled in USDC, decided at the **fill boundary** by whether the fill **took** liquidity
(crossed on arrival) or **made** it (rested), computed there (paper: `notional × maker/taker rate`
on the instrument; live: read from the venue) and accrued as its **own ledger line**, never folded
into entry price or realized PnL. **Making liquidity is not what makes the fee negative**: the base
maker rate is a positive cost, and a rebate is a property of the account's volume tier — a measured
maker fill (`crossed: false`) carried `fee` **`+0.019571`**, the base `0.015 %` (ADR-0036, #152).
See ADR-0036, ADR-0013.
_Avoid_: commission (a taken-liquidity synonym; "fee" spans rebates too), cost basis (that's entry
price), slippage (a fill-*price* effect, not a fee).

**Funding**:
The periodic **cash adjustment** a perpetual [[Position]] accrues to its [[Account]] collateral —
a signed `Decimal` (negative = **paid**, positive = **received**, mirroring the venue's
`userFunding.usdc`), settled hourly at epoch-aligned boundaries as **`FundingAccrual`** events
`(account, symbol, boundary_ts, amount)`, keyed idempotent so catch-up, reconcile, and restart
converge. Paper **generates** it on the [[Clock]] cadence (`amount = − signed_size × price ×
funding_rate`, `funding_rate` a per-boundary rate on the instrument); live **ingests** the venue's
reported payment. Its **own ledger line**, never entry price or realized PnL. See ADR-0037, ADR-0034.
_Avoid_: interest, carry, funding **fee** (it is not a [[Fee]] — no trade, no maker/taker),
funding **rate** (the input rate, not the cash accrual).

**Notional**:
A [[Position]]'s gross market value — **unsigned**: `|size| × mark`, the magnitude **that**
`maintenance_margin`, **cross** `margin_used` and [[Leverage|effective leverage]] are computed from
(an **isolated** position's `margin_used` is computed too, but from its own collateral and uPnL
rather than from notional — [[Margin]]). The
[[Account]] total is the **sum of those magnitudes** — *gross*, never net: two opposite positions
of equal size total twice one of them rather than zero, because each independently ties up
collateral and each is independently liquidatable. Tier-2. See ADR-0045, ADR-0040.
_Avoid_: **net exposure** / net notional (a signed portfolio-risk quantity, and the deferred
RiskEngine's concern — ADR-0017; this word is a magnitude), position value, exposure unqualified.

**Margin** *(reported)*:
The **reported** collateral a [[Position]] ties up — `margin_used`, with its sibling
`maintenance = notional × margin_maint` at a flat tier-0 rate **exact only below the asset's first
margin-tier band** (above it the venue charges `notional × mmr(tier) − deduction(tier)`, and the
flat rate under-reports — ADR-0040 §4) — **never enforced**: the
[[Paper exchange]] never rejects an order for margin and never liquidates (a future map). What
`margin_used` *is* depends on the mode, but **both are Tier-2, recomputed each read**: a **cross**
position shares one account pool and computes `notional / leverage`; an **isolated** position
computes `isolated_collateral + uPnL` — its backing collateral plus its unrealized PnL, which moves
with the mark. The `isolated_collateral` underneath it *is* Tier-1 and **persisted** (static at open
on paper, ingested on live as `marginUsed − unrealizedPnl` — never the venue's `rawUsd`, which is
the cash leg net of cost basis and is negative for a long). `max_leverage` and `margin_maint` are
additive `InstrumentSpec` fields. Both `margin_used` computations sit inside ADR-0040 §6's alert
band. The account-level **maintenance** total is reported over every position but **cross-checked
only over the cross subset** — the venue's `crossMaintenanceMarginUsed` excludes isolated positions,
which have no venue maintenance counterpart at all (ADR-0046 §2.1); `margin_used`'s account total
needs no such narrowing, since `marginSummary.totalMarginUsed` includes them.
See ADR-0040, ADR-0041, ADR-0043, ADR-0045, ADR-0046, and the [#142](https://github.com/MarcosACH/tickwright/issues/142)
testnet measurement that settled the tiering.
_Avoid_: **initial margin** (conventionally the collateral reserved when an *order* is submitted —
an admission gate this surface does not implement), margin **call**, buying power
(enforcement/broker terms — this surface only reports), margin **tier** (the piecewise table is a
deferred extension point).

**Leverage** & **Margin mode**:
A **per-symbol / per-position** input (not an [[AccountSpec]] fact): the integer leverage and
`cross`/`isolated` mode that set a [[Position]]'s [[Margin|margin_used]], carried together as one
`LeverageSpec` because the venue sets them in one action. **Config-authoritative on both paths** —
declared venue-agnostically in `AppConfig.leverage` (default **1x / isolated**, the safest pair) and
live-ingested as a cross-check. What an operator writes is **sparse** and what both consumers receive
is a **`LeverageBook`** — the same map completed over `AppConfig.traded_symbols`, so an unnamed traded
symbol carries the default rather than a hole; the composition root does the completing, because it
is the only scope holding the strategy-declared set. The book also owns §9's bound
(`1 ≤ leverage ≤ max_leverage`, refused in `Exchange.start()` on both paths). The engine **pushes** it to the venue **once, at boot** (ADR-0044):
symbols already aligned are skipped, symbols holding no position are written blind, and a
disagreement on a symbol that *does* hold a position **refuses to start** rather than re-margining a
live position — after which the venue is left alone for the run, with drift **alerted, never
re-pushed**, on a direct exact-match check (`LEVERAGE_DIVERGENCE`; the indirect `margin_used` route is
blind for **any** position held across the drift, because a leverage change never re-margins an open
one — measured isolated, inferred for cross, #142 and ADR-0044 §10). A
boot-time push is safe under every venue branch: a change on a held position cannot silently
re-margin it, a **mode switch** on one is always rejected, and a **decrease** only succeeds when the
locked collateral allows. **Effective leverage** is a convention-only readout with no venue
counterpart — the *realized* ratio (vs the set nominal `leverage`),
`notional / (isolated_collateral + uPnL)` for an isolated position (so adding isolated margin lowers
it — measured: a +20 USDC top-up drove it 5.0119 → 2.8260) and `notional / equity` for
cross/account, **`None` when that denominator is `≤ 0`**; the isolated denominator was a modelling
choice R3 flagged for confirmation and [#142](https://github.com/MarcosACH/tickwright/issues/142)
**confirmed** (ADR-0041 §4.1). See ADR-0040, ADR-0041, ADR-0038, ADR-0044.
_Avoid_: account leverage (it is per-symbol); **margin** as the name for this input (that word is
this glossary's [[Margin]] — the collateral a position ties up, *computed* in both modes, off the
nominal leverage on a cross position and off the ingested collateral **plus uPnL** on an isolated
one; this is its *setting*); topping up isolated collateral (`updateIsolatedMargin` — a deferred
extension point, ADR-0044 §8).

**Liquidation price**:
The per-[[Position]] price at which the venue would liquidate it — **nullable**, and the **one**
Tier-2 number not computed everywhere: **read-through on live** (the venue's `liquidationPx`,
stale-frozen, `None` when absent) because re-deriving it needs the maintenance-margin tier fixed
point, **computed on paper** from the canonical formula. Never enforced here. `None` is **routine,
not exceptional**: the venue reports no liquidation price when it would be **non-positive**, which is
reachable for a long once collateral is large relative to notional and *impossible* for a short —
measured at 12 of 17 cross longs. Paper mirrors that rule, reporting `None` on a computed
`liq_price ≤ 0` (ADR-0046 §6). See ADR-0040, ADR-0034, ADR-0046.
_Avoid_: stop-out, margin-call price; account liquidation price (there is none — it is per-position).

**Equity** & **Free margin**:
The [[Account]]'s reported collateral numbers: **`equity = cash + Σ unrealized_pnl`** — the Tier-1
cash line plus the Tier-2 valuation, true on both paths — and
`free_margin = equity − total_margin_used` (isolated buckets locked, excluded). The **cash line**
accrues from four signed inputs — [[Genesis collateral|genesis]], `+` realized PnL, `−` [[Fee|fees]]
(the fee term **subtracts**, a [[Fee]] being a cost magnitude: `> 0` debited, `< 0` a maker rebate
credited, ADR-0036, while realized PnL and [[Funding]] are already signed deltas and add),
`+` funding — with one standing exception on live: the reconciler's cash adjustment corrects that
line toward venue truth without accruing from anything the engine did (ADR-0034). So the four-input
sum is how cash **moves**, not a formula equity is defined by. A **negative** free margin is
**reported without consequence** (no reject, no liquidation, no alert) — the honest "underwater on
live" signal. On live the two are **cross-checked** against `marginSummary.accountValue` and
`crossMarginSummary.accountValue − crossMarginSummary.totalMarginUsed` respectively — never against
the venue's `withdrawable`, which is a *different quantity* (ADR-0046 §2). Both comparisons run over
**all** positions: the free-margin pair is cross-scoped on the venue's side, but the isolated term it
drops cancels in the difference, so — unlike the maintenance total above — our side is not narrowed
to match (ADR-0046 §2.1).
See ADR-0045, ADR-0040, ADR-0042, ADR-0046.
_Avoid_: balance (`cash` is one term of equity), buying power, **withdrawable** (not a synonym —
the venue's `withdrawable` deducts from `accountValue` **whichever is larger** of the account's
total initial margin — positions *and* exposure-increasing resting orders — or a 10 %-of-notional
withdrawal floor: `max(0, accountValue − max(initial_margin, 0.1 × totalNtlPos))`, a `max` and not a
sum, with the floor the term that usually binds. This surface models neither the order-margin
component nor the floor; ADR-0046 §2),
**position equity** (equity is account-grain; an isolated position's backing collateral is named
descriptively — ADR-0041 §4.1).

**Genesis collateral**:
The value the [[Account]]'s cash line opens at — the one number equity, free margin and effective
leverage are measured against. On **paper** it is operator-declared: a strictly positive
`PaperExchangeConfig` field with no default, **demanded by `AppConfig` whenever the paper exchange is
selected** (never required at field level, which would drag a paper number into a live run), because a
non-zero default would report against capital nobody chose. On **live** it is **ingested** inside the
**startup reconciliation barrier** — not at a later cadence reconcile, which would start strategies
with no account row at all — as `accountValue − Σ unrealized_pnl` (`accountValue` is equity and
already contains uPnL, so the subtraction is what stops it being double-counted). Paper's is written
a step earlier still, seeded by the startup check that would otherwise refuse the store (ADR-0043
§6). Persisted as its own column on both paths — beside the instant it was written — and
distinct from the cash line that accumulates away from it; on paper a config value disagreeing with
the stored one **fail-fasts** alongside the [[AccountSpec]] `account_id` check — a different genesis
is a different account history. Together with realized PnL, [[Fee|fees]] and [[Funding]] it closes
the cash line's write-set at four **accruing** inputs — three added and fees subtracted — while the
reconciler's synthetic cash adjustment (ADR-0034) corrects that line on live but accrues nothing to
it: deposits, withdrawals and transfers
are not modelled, and a real one on live surfaces as a benign Tier-1 divergence that heals and
alerts. See ADR-0042, ADR-0043, ADR-0040.
_Avoid_: starting balance, initial deposit (nothing is deposited — the account is declared, not
funded), seed capital.

## Relationships

- The **Engine** hosts one **EventBus**; swapping the bus backend (InMemory ↔ Kafka) changes
  durability and inspectability, never the number of processes.
- The **Engine** hosts exactly one live **Exchange** = **one venue per process** and **one Account
  per process**; scaling to N exchanges or N accounts is N processes ([[Venue adapter]]), not one
  engine routing across venues or accounts. An **Account** is owned by exactly one process
  (ADR-0038) — two engines on one account would each heal their ledger toward the other's flow.
- Concrete impls ([[Venue adapter]]s, bus/store backends, strategies) depend only on the `domain`
  Protocols; the [[Composition root]] is the one place that knows every concrete
  ([[Dependency direction]]).
- A **Position** belongs to one **Account** and one **Strategy** — or to the unattributed partition
  when the engine did not place the flow; on a `NET` venue
  `Σ(Position size per symbol) = Account net size = venue szi` holds by construction
  (per-strategy attribution bridged to the reconciliation anchor, ADR-0034/0038).
- Each **Venue adapter** declares its **AccountSpec**; the **Engine** wires it in at startup, the
  same way it wires the instrument specs into the **PreTradeGuard** (ADR-0031, ADR-0038).
- The **PortfolioProjection** projects **Position**/**Account** state and implements the **Portfolio**
  seam, fed by the **ExecutionManager** on the fill-apply path — the accounting sibling of the
  **Cache** (order read-model).
- Both read-models are built and written by one **Checkpointer**, which the **Engine** constructs
  from the one **Store** it was given; the **ExecutionManager** takes that single collaborator
  rather than a store, a cache and a projection it would have to keep pointed at one another.

## Flagged ambiguities

- "worker" in the author's prior system meant a separate OS process per pipeline stage. Here
  the whole pipeline is one process; avoid "worker" for Tickwright components — use the
  component name (feed/strategy/exchange) or **Engine** for the host.
- "Portfolio" was used for both the accounting read-facade and the deferred portfolio-*risk*/exposure
  surface — resolved: [[Portfolio]] is the accounting read seam (this map); portfolio-risk enforcement
  stays the deferred RiskEngine concern (ADR-0017).
- "collateral" carries three distinct senses and deliberately has **no term of its own**: the
  account's collateral *pool* (→ [[Account]], [[Equity]]), an isolated position's *locked* collateral
  (→ [[Margin]], [[Leverage]]), and the account's *opening cash line* (→ [[Genesis collateral]]).
  Each sense is owned by the term it belongs to — a fourth, generic definition would overlap all
  three and have to be kept in sync with each (ADR-0045). Always say which.
