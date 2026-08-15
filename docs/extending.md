# Extending Tickwright

Tickwright is extended by **implementing a Protocol** — never by registering a plugin or wiring a
config-DSL (that's a deliberate non-goal; see the [README](../README.md)). Every swappable boundary
is a structural `Protocol` in [`domain/protocols.py`](../src/tickwright/domain/protocols.py): an
adapter satisfies a seam *by shape*, so your implementation imports `domain` and nothing imports it
back. Selection is one explicit `match` arm in the composition root
([`app/build.py`](../src/tickwright/app/build.py)) keyed off a `Literal` discriminant in
[`app/config.py`](../src/tickwright/app/config.py).

The seams and their two shipped implementations:

| Seam (`Protocol`) | Discriminant | Shipped impls |
| --- | --- | --- |
| `Strategy` | `strategies[].kind` | `single_shot_market`, `single_shot_limit` |
| `MarketFeed` | `feed` | `replay`, `hyperliquid` |
| `Exchange` | `exchange` | `paper`, `hyperliquid` |
| `EventBus` | `bus` | `in_memory`, `kafka` |
| `Store` | `store` | `sqlite`, `postgres` |
| `PreTradeGuard` | `guard` | `real`, `noop` |
| `Clock` | *(derived from `feed`)* | `LiveClock`, `ManualClock` |
| `FillModel` (paper-internal) | `paper.fill_model` | `immediate`, `stochastic` |

> **Two implementations per seam — no more.** One looks hardcoded; three is scope creep. The repo
> ships exactly two of each on purpose. Your extension is the *third* — keep it out of tree, or
> replace one of ours, but don't grow the shipped set.

---

## The pull-then-subscribe strategy contract

This is the one contract a strategy author **must** internalize. It is the reason a strategy can be
trivially small and still be crash-safe.

A `Strategy` is subscribed to the `EventBus` **last** — after the engine's startup sequence has
recovered durable state, rebuilt the `Cache`, cleared the reconciliation barrier, and (only then)
started the feed (ADR-0024). Everything that happened during startup — including the reconciler's
synthetic `OrderEvent`s that heal missed fills — was published **before your handlers were
subscribed**, and on the `InMemoryBus` those events are already gone (at-least-once pub/sub, no
per-consumer replay). So:

> **Never rely on having seen an event published before you subscribed.** Reconstruct "what is true
> now" by *pulling* current state, then *subscribe* for the deltas that follow.

Concretely, the engine does the pulling **for** you before it subscribes you (in `StrategyHost`):

1. **`restore(snapshot_bytes)`** — your last `snapshot()` is fed back so you rebuild your own state
   *content*. Returning bad/old bytes is not fatal: the engine logs
   `strategy.snapshot_incompatible` and starts you fresh (ADR-0016). Never let seq-safety depend on
   the snapshot.
2. **`set_next_seq(next_seq)`** — your monotonic `seq` is resumed from the **saga store's
   high-water mark**, not from your snapshot, so a stale snapshot can never reuse a consumed
   `signal_id` (ADR-0006/0016). Both place *and* cancel intents consume a `seq`.
3. Only then are your `on_tick` / `on_order_event` handlers subscribed, and the feed starts.

Because current open-order truth lives in the `Cache` read-model (a write-through projection of the
`Store`, rebuilt during recovery — never the source of truth), a *stateful* strategy reads it there
rather than reconstructing it from a stream it never saw. The corollary discipline:

- **Keep state minimal and reconstructible.** Version your snapshot payload (`{"version": 1, …}`)
  and reject unknown shapes in `restore()` — the engine turns that into a clean start.
- **Signal ids must be deterministic and gap-free**, driven only by the engine-set `seq` — replay or
  restart must produce the same `signal_id`, or idempotent recovery breaks (ADR-0006).
- **Handler exceptions are contained, not fatal.** The `StrategyHost` catches them, emits a
  correlated `strategy.error`, and continues; only an `InvariantViolation` pierces. Don't swallow
  errors trying to "protect" the engine — that's the host's job.

---

## Checklist: add a `Strategy`

A strategy consumes ticks and lifecycle events and emits `PlaceSignal`/`CancelSignal`s. It depends on
`domain` **only** — no saga, store, or venue knowledge. Study
[`strategies/single_shot.py`](../src/tickwright/strategies/single_shot.py) as the reference.

- [ ] Add a class in `src/tickwright/strategies/` implementing the `Strategy` Protocol:
  `strategy_id`, `on_tick`, `on_order_event`, `set_next_seq`, `snapshot`, `restore`.
- [ ] Emit signals through a composed `SignalEmitter`
  ([`strategies/emitter.py`](../src/tickwright/strategies/emitter.py)) — it owns the `seq`,
  clock-stamps each signal, and publishes it. Never build a `signal_id` by hand.
- [ ] Make `snapshot()`/`restore()` a versioned, minimal payload; raise on an unknown version.
- [ ] Add a `kind` value to the `StrategyConfig.kind` `Literal` in
  [`app/config.py`](../src/tickwright/app/config.py), plus any config fields it needs (validate
  cross-field requirements in the model, as `single_shot_limit`'s `price` does).
- [ ] Add one `match` arm to `_build_strategy(...)` in [`app/build.py`](../src/tickwright/app/build.py).
- [ ] TDD it: a `ReplayFeed` tick → your strategy → `PaperExchange` → the expected `OrderEvent` on the
  bus, plus a snapshot/restore round-trip and a seq-resumes-after-restart test.
- [ ] Document the new `kind` in [`.env.example`](../.env.example) (`TICKWRIGHT_STRATEGIES`).

## Checklist: add a venue package

A venue is the **extension unit** (ADR-0031/0032): one self-contained package under
`src/tickwright/venues/<venue>/` owning *all* venue knowledge — symbol/asset mapping, endpoints,
auth, quirk translation — and importing no other adapter. It provides both a `MarketFeed` and an
`Exchange`, and sources its own `InstrumentSpec`s. Study
[`venues/hyperliquid/`](../src/tickwright/venues/hyperliquid/) as the reference.

- [ ] Create `src/tickwright/venues/<venue>/` with a `MarketFeed` adapter (`start`/`stop`, publishes
  `MarketTick`s **and `MarkTick`s** — the mark is market data and enters here, never off a reconcile
  pull, so a feed that omits it leaves every Tier-2 valuation reading `None`, ADR-0039), an
  `Exchange` adapter (`start`/`run`/`stop` plus
  `place`/`cancel`/`fetch_order`/`fetch_account_state`/`account_spec`/`instrument_specs`), spec
  sourcing, and a `<Venue>Config`.
- [ ] Honor the `Exchange` contracts: a failed read is **never venue truth** (never `[]`, never a
  view — an outage must not look like "no orders", ADR-0011 inv 1), and the two read grains say so
  differently. `fetch_order` returns a **`VenueReadFailure`**, whose member says *which way* it
  failed: `SEND_FAILED` when no body arrived (the reconciler stops its whole pass — the venue may
  be unreachable, and every order behind this one would pay a full request timeout to learn the
  same) and `UNREADABLE_BODY` when a body arrived and could not be parsed (only that order is
  skipped, against a per-cloid span that faults once the condition is proven durable, ADR-0049).
  `fetch_account_state` reads one grain with no worklist behind it, so it collapses both and
  returns **`None`**. `place`/`cancel` emit raw `ExecutionReport`s on the bus rather than
  returning them; a cancel of an unknown order is a benign no-op.
- [ ] Put venue alignment in `start()`, a loop of your own in `run()`, and release in `stop()` —
  never in `__init__` or a placement.
  The runner drives `start()` at ADR-0024 step 4 — after the bus, **before** the startup barrier — so
  a refusal there (an `InvariantViolation`) faults the process before any order can go out, and the
  barrier reads an already-aligned venue. Retry a transient venue blip inside the
  `startup_reconciliation_timeout` budget yourself; the runner does not retry the call, so raising
  spends the last of it. That budget is **yours to enforce** — the runner neither retries nor bounds
  `start()`, so it **must not hang**: a wedged boot has no bound and no operator escape (the task
  that watches SIGINT is not created until the start sequence returns, so SIGKILL is the only way
  out). Put a timeout on any blocking venue call you make here.
  `run()` is the **supervised long-lived half** — the peer of `MarketFeed.start()`, one seam over.
  Anything of yours that loops for the life of the run goes here and nowhere else: the runner
  task-creates it inside its `TaskGroup`, so a failure in it aborts the group and faults the engine
  **at the moment it happens**. A loop you spawn for yourself in `start()` has no fault channel at
  all — it dies alone while the engine runs on, accruing nothing, and exits 0
  ([#226](https://github.com/MarcosACH/tickwright/issues/226)). `start()` cannot be that loop: the
  runner awaits it inline and it must return for the barrier to run. Returning at once is the
  common case and a legitimate implementation rather than an omission — an adapter that only
  answers requests has nothing to supervise, which is `HyperliquidExchange`; `PaperExchange`
  settles ADR-0037's funding boundaries there, because a venue with nobody to ask for funding has
  to generate it. Note the instant: the `TaskGroup` opens *behind* the barrier, so anything `run()`
  publishes is produced after reconciliation cleared.
  `stop()` is driven once the feed is cut **and** the reconcile cadences are
  cancelled — nothing is left to call `fetch_order` on you — and still ahead of the bus drain,
  because your `run()` loop — which the runner cancels in this same slot, not you — would otherwise
  publish into that drain and keep raising its high-water mark, so it would never quiesce. Let that
  cancellation through: `CancelledError` is the ordinary end of `run()` and must not be caught.
- [ ] Write `stop()` to tolerate all three edges the runner's teardown creates. A `start()` that
  never ran or refused (the fault path releases either way). A **second** call — one membership is
  walked twice, the faulted pass restarting at the top, so a graceful step that raises behind you
  drives you again; make it a no-op, not a failure. And a **late `place`/`cancel`**: the drain runs
  behind your release and still dispatches, and the strategies stop one step later still, so an
  in-flight tick can turn into a `Signal` after you have released. Keep those two answerable —
  refuse cleanly on a dead link, never hang, or you wedge the drain you were released ahead of.
- [ ] Keep secrets env-only: never persist a signing key, and register it for log redaction.
- [ ] Add the venue to the `feed` and `exchange` `Literal`s in
  [`app/config.py`](../src/tickwright/app/config.py).
- [ ] Add one `match` arm each to `build_feed(...)` and `build_exchange(...)` in
  [`app/build.py`](../src/tickwright/app/build.py), plus the clock pairing in `build_clock(...)` if the
  feed drives virtual time (a live feed uses `LiveClock`).
- [ ] Confirm the import boundary: `uv run lint-imports` must pass — the package imports `domain` and
  `observability` only, never `engine` or another adapter.
- [ ] TDD the adapters at their own seam, and give the `Exchange` half a **claim per member**:
  `assert isinstance(exchange, Exchange)`, plus a `_SEAM_CLAIMS` map — member → the test that says
  what that member does on *your* venue — handed to `assert_every_member_is_claimed`
  ([`tests/_support/seam_claims.py`](../tests/_support/seam_claims.py)), as both shipped adapters do.
  The `isinstance` check alone cannot be that gate: you must implement every member for the engine
  to run at all, so it goes green the moment the member exists while nothing asserts what it *does*.
  The gate is member-grained, not clause-grained — it catches a forgotten member, never a forgotten
  clause, so the three `stop()` edges above still need a reviewer.
- [ ] Document the new config in [`.env.example`](../.env.example), and add an ADR if the integration
  makes a load-bearing decision.

## Checklist: add a bus or store backend

The `EventBus` and `Store` seams are pure infrastructure swaps — same interface, different durability.
"Swapping the backend changes durability, never behavior." Study
[`adapters/bus/`](../src/tickwright/adapters/bus/) and
[`adapters/store/`](../src/tickwright/adapters/store/) as the reference.

- [ ] Add the adapter under `src/tickwright/adapters/{bus,store}/` implementing every method of the
  `EventBus` / `Store` Protocol.
- [ ] Preserve the shared bus contract: **at-least-once** delivery, **per-symbol** ordering only
  (`partition_key`), pub/sub only (no query surface — queries are direct method calls, ADR-0004). A
  `Store` stays synchronous (the checkpoint is one atomic, no-yield step) and holds only the three
  things ADR-0019 allows (saga records, strategy snapshots, kill-switch state).
- [ ] Add the discriminant to the `bus` / `store` `Literal` in
  [`app/config.py`](../src/tickwright/app/config.py), and a `<Backend>Config` in the adapter's package.
- [ ] Add one `match` arm to `build_bus(...)` / `build_store(...)` in
  [`app/build.py`](../src/tickwright/app/build.py). Import a heavy driver **inside** the arm, not at
  module top, so selecting the hermetic default never loads it (see the `kafka`/`postgres` arms).
- [ ] TDD it against the same behavioral contract the shipped backends are tested against, so
  delivery/durability parity is *demonstrated*, not assumed.
- [ ] Document the new config in [`.env.example`](../.env.example).

---

## Why one `match` arm, and nowhere else

Exactly one module — the composition root — knows every concrete
([ADR-0032](adr/0032-package-topology-dependency-direction-composition-root.md)). Adding an impl is
one `Literal` value plus one `match` arm there; nothing in `engine` or another adapter changes,
because they only ever see the Protocol. `match` over an `assert_never`-closed `Literal` means a new
discriminant that you forget to wire is a **compile-time** (mypy) error, not a runtime surprise. That
is the whole extensibility story — no registry, no entry-point discovery, no import-path DSL.

See [`CONTEXT.md`](../CONTEXT.md) for the vocabulary and
[`docs/module-maps/v1-core-engine.md`](module-maps/v1-core-engine.md) for each module's full
interface and depth rationale.
