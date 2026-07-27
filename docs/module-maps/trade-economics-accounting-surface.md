# Module Map: Trade economics — the venue-agnostic accounting/portfolio surface (perps)

## Source

PRD: [#168 — Trade economics: the venue-agnostic accounting/portfolio surface (perps)](https://github.com/MarcosACH/tickwright/issues/168).
Phase 0: wayfinding map [#107](https://github.com/MarcosACH/tickwright/issues/107), 19 decision tickets, ADRs **0034–0046**.
Constraints inherited from ADR-0032 (dependency direction, venue-as-extension-unit, composition root), ADR-0035 (topology and placement — which names this folder tree as its own artifact), and ADR-0015 (execution topology). Terms are used exactly as defined in `CONTEXT.md`.

Top-level layout — additions to the [v1 core-engine map](./v1-core-engine.md), which stands unchanged:

```
src/tickwright/
  domain/
    position.py        # Position aggregate + PositionView                    NEW
    account.py         # Account aggregate + AccountSpec + AccountView        NEW
    leverage.py        # MarginMode + LeverageSpec                            NEW
    economics.py       # fee / funding boundary helpers (pure)                NEW
    valuation.py       # Tier-2 view assembly (pure)                          NEW
    events.py          # + MarkTick, FundingAccrual, fee, VenueAccountState
    instrument.py      # + maker_fee, taker_fee, funding_rate, margin_maint, max_leverage
    protocols.py       # + Portfolio; Exchange ×4; Store ×5
    errors.py          # + StoreAccountMismatch, VenueLeverageMismatch,
                       #   VenueAccountModeUnsupported
  engine/
    portfolio.py       # PortfolioProjection + ScopedPortfolio + mark cache   NEW
    ledger_reconcile.py# LedgerReconciliation — the account-grain heal cycle  NEW
    execution.py       # + the atomic fill checkpoint
    cache.py           # + project() (in-memory half, for the atomic path)
    strategy_host.py   # + symbol-ownership and __unattributed__ fail-fasts
    runner.py          # + recover step, exchange.start/stop, barrier
                       #   materialisation, the ledger cadence
  adapters/
    paper/funding.py   # Clock-driven boundary generator with catch-up        NEW
    paper/exchange.py  # + fee at the fill boundary, account_spec,
                       #   fetch_account_state, start/stop
    feed/replay.py     # + a MarkTick per row
    store/             # + three tables, checkpoint_ledger, four reads
  venues/hyperliquid/
    account.py         # clearinghouseState -> VenueAccountState, AccountSpec NEW
    preflight.py       # userAbstraction gate + updateLeverage push split     NEW
    funding.py         # userFundings ingest, batch sort                      NEW
    exchange.py        # + start() orchestration, feeToken guard
    feed.py            # + activeAssetCtx -> MarkTick
  strategies/          # + a Portfolio constructor param on single_shot.py —
                       #   the seam's only consumer (ADR-0041 §8)
  app/                 # + AppConfig.leverage, paper genesis/label, the
                       #   resolved leverage map, Portfolio facade injection
  observability/       # + the accounting named events
```

## Decisions this map fixes

The ADRs deferred these to `/module-map` by name. They are decided here and are binding on the slices.

1. **The folder tree above** — ADR-0035 *Placement* ("the concrete folder tree … is a `/module-map` artifact").
2. **The reconcile pull is `Exchange.fetch_account_state() -> VenueAccountState | None`** — one call, one response, the connectivity guard in the return type (PRD *Further Notes*: no ADR names it). Putting it on the shared seam obliges paper to answer, so this map also fixes what paper answers: **`None`, always**, because `PaperExchange` has no account truth and `None` is the only fail-closed value (see `paper`).
3. **Tier-2 assembly is a pure `domain/valuation.py`**, not aggregate methods. ADR-0041 §4 computes the position-grain half off the symbol's **account-net** size — a computed aggregation over partitions (ADR-0035), not a field of any one `Position` — and every cross-mode quantity reads account equity, which is `cash + Σ uPnL` over the whole set. Both couplings turn an aggregate method's argument list into the rest of the model. The aggregates keep only the queries that read their own state and a mark.
4. **The ledger heal cycle is its own module**, not a third cadence on `Reconciliation`: a different anchor (account snapshot vs cloid), a different freeze grain, its own alert types, and a mode re-verification the order reconciler knows nothing about (ADR-0041 §8: "the projection's own healing loop").
5. **`Exchange` gains `stop()`** so the paper funding loop has an owner and a cancellation point. **Docs-sync**: ADR-0044 §7 left `stop()` undeclared "until there is teardown to do" — this is that teardown; annotate it in the slice that lands the loop.
6. **The `ExecutionManager` makes the single `checkpoint_ledger` call** on the fill path. `Cache.checkpoint` keeps the store write for every non-fill transition (none touches the ledger); the fill path writes all three aggregates in one call and then projects both read-models in memory.
7. **The Hyperliquid venue package splits three ways** — `account.py` (read/normalize), `preflight.py` (the two boot guards), `funding.py` (ingest) — with `exchange.py` orchestrating only.

## Modules

### Position (`domain/position.py`)

**Interface:** `Position` — the mutable per-`(strategy, symbol)` economic aggregate, the economic sibling of `Order` (ADR-0035). `strategy_id` is `str | None`, `None` being the reserved unattributed partition (ADR-0038). Callers must know: `apply(event)` is **idempotent** and tracks applied event ids (ADR-0025), so a redelivered fill, a reconciler synthetic and a restart replay all converge; it raises `InvariantViolation` on an illegal application, never a silent skip. Tier-1 fields (`signed_size`, `entry_price`, `realized_pnl`, `fees`, `funding`, `isolated_collateral`) are exact and **never `None`** on the view; `entry_price` resets on a full close and a flip-through-zero realizes the whole closed leg before opening the residual fresh at the fill price (P1 [#119](https://github.com/MarcosACH/tickwright/issues/119)). Realized PnL is **gross** of fees and funding (ADR-0045 §3). Query methods are limited to what the position's own state plus a mark answers — own-slice `unrealized_pnl(mark)`, cost basis; everything account-net- or pool-coupled lives in `valuation.py`. `PositionView` is the frozen read-only snapshot the seam returns, carrying the two grains ADR-0041 §4 fixes (own-attribution slice + position-grain economics + `mark_ts`).

**Responsibilities:** average-cost accounting; the flip-through-zero rule; realized-PnL signing against closed exposure; the fee and funding ledger lines kept separate from entry price and from realized PnL; idempotency bookkeeping.

**Seams:** None — a pure `domain` value type, like `Order`.

**Depth note:** The deepest new unit. Delete it and the average-cost algorithm scatters into the projection, the reconciler's synthetic path and every test that needs a position — and the one algorithm the whole surface's correctness rests on stops being property-testable without infrastructure. It is the module the `hypothesis` suite (associativity under fill reordering, duplicate-fill convergence, Tier-1 exactness under quantization) stands on.

---

### Account (`domain/account.py`)

**Interface:** `Account` — the mutable single-account aggregate: `account_id`, `genesis_collateral`, `genesis_ts_ns` (all three written once and never moved), and the Tier-1 `cash` line. Callers must know: **`cash` has exactly four accruing inputs** — genesis, `+` realized PnL, `−` fees, `+` funding (ADR-0042 §4) — plus one live-only correction, the reconciler's synthetic cash adjustment, which is not a fifth input; `apply()` is idempotent on the same terms as `Position`. `AccountSpec` is the frozen venue-declared value the `Exchange` exposes: `account_id` (venue + network + venue-native identifier), `netting` (`NET`/`HEDGE`), and `genesis_collateral: Decimal | None` — the operator's declared number on paper, `None` on live, which is the predicate both startup checks read (ADR-0043 §10). `AccountView` is the frozen account-wide pool snapshot (ADR-0041 §4); it carries no realized PnL and no liquidation price, both being wrong at that grain.

**Responsibilities:** the cash line and its closed write-set; the account's immutable opening declaration; the venue-declared static facts; the account-grain view shape.

**Seams:** None. `AccountSpec` is *consumed* at the `Exchange` seam, not a seam itself.

**Depth note:** Passes the deletion test through the write-set: without one owner of "what may move cash", the four accruing inputs and the one correction spread across the fill path, the funding path and the reconciler, and the Σ-invariant stops being checkable. Carrying `genesis_collateral`/`genesis_ts_ns` here is what lets `load_account()` gate the never-updated rule through the shared store-contract suite rather than per-backend SQL (ADR-0043 §9).

---

### Leverage (`domain/leverage.py`)

**Interface:** `MarginMode` (`cross` | `isolated`) and `LeverageSpec` — a frozen `(mode, leverage)` pair, defaulting to the safest combination `1x` / `isolated`. Callers must know: mode and leverage are **one value, not two maps**, because `updateLeverage {asset, isCross, leverage}` sets both in one signed action and splitting them would invent a state the venue cannot express (ADR-0044 §2). It is **venue-agnostic and per-symbol** — deliberately not on `InstrumentSpec` (which stays identical venue metadata across paths) and not on `AccountSpec` (the venue keys it per asset). The map the engine receives is **complete**, never the sparse `AppConfig.leverage`: the composition root resolves the two into every strategy-traded symbol before injecting it.

**Responsibilities:** the one operator-authored input to the margin model, in the shape the venue action carries.

**Seams:** None.

**Depth note:** Small but load-bearing for the identical-compute grain: one value read by both the venue-agnostic `PortfolioProjection` and the venue `Exchange`, so paper and live cannot compute margin off different leverage. Its own module because it is the only *input* value in a package otherwise made of outputs — filing it under `position.py` or `instrument.py` invites exactly the confusion ADR-0040 §5 and ADR-0044 §2 spent two sections preventing.

---

### Economics helpers (`domain/economics.py`)

**Interface:** Pure functions at the **fill and accrual boundary**, peers of `quantize_size`/`below_min_notional` (ADR-0036/0037): the signed fee for a fill given its maker/taker side and an `InstrumentSpec`; the funding amount `− signed_size × price × funding_rate`; and the epoch-aligned funding boundaries strictly crossed between two instants. Callers must know: the fee sign convention is `> 0` cost debited, `< 0` maker rebate credited (ADR-0036 — and a maker fill is *not* what makes it negative); the funding amount mirrors `userFunding.usdc`, negative = paid; boundaries are measured from the Unix epoch, so a replayed run crosses the same absolute boundaries a live run would.

**Responsibilities:** the arithmetic each `Exchange` adapter runs at its boundary, shared so paper computes what live reports in the same shape.

**Seams:** None — and deliberately not a `FeeModel`/`FundingModel` seam (see *Out of scope*).

**Depth note:** Thin by construction, but it is where the "no money math in the matching path" line of ADR-0013 is kept: the paper `FillModel` still emits price + quantity only, and the fee is computed *after* matching, here.

---

### Valuation (`domain/valuation.py`)

**Interface:** Pure functions assembling the two frozen views from an explicit input set — `position_view(position, account_net, account, mark, mark_ts, leverage, spec)` and `account_view(account, positions, marks, leverage, specs)`. Callers must know: every field of one view comes from **one** `(position, mark)` read, which is what makes a view internally coherent by construction (ADR-0041 §1); nothing here is stored, ever (ADR-0034's Tier-2 rule, ADR-0043 §3's "no Tier-2 value is ever persisted"); the nullability rule is **per-term, not per-field** — a field reads `None` only when the mark is absent *and its own terms need it*, so a flat position still reads `0` (ADR-0041 §6); `effective_leverage` is additionally `None` on a non-positive denominator, the one Tier-2 `None` a fresh mark cannot cure; a computed `liquidation_price ≤ 0` reads `None`, mirroring the venue's own majority case for a long (ADR-0046 §6).

**Responsibilities:** `notional`, `unrealized_pnl`, `equity`, `margin_used` (cross `notional / leverage`, isolated `isolated_collateral + unrealized_pnl`), `maintenance_margin`, `free_margin`, `effective_leverage` (per-position denominators split by mode, ADR-0041 §4.1), and the paper `liquidation_price` formula. On live `liquidation_price` is ingested rather than computed and is passed through (ADR-0040 §3).

**Seams:** None.

**Depth note:** This is the map's formula-dense core, and putting it behind a pure function signature is what makes it testable at all: every quantity the [#142](https://github.com/MarcosACH/tickwright/issues/142)/[#152](https://github.com/MarcosACH/tickwright/issues/152) testnet measurements pinned — the liquidation price matched to 28 significant figures, the isolated `margin_used` identity, the `maintenance_margin` flat rate across 50 symbols — becomes a table-driven unit test with no engine, no store, and no venue. Delete it and those formulas live inside a projection that needs a fixture to reach, which is the difference between a reference implementation and a plausible one.

---

### `domain` extended surfaces (`protocols.py`, `events.py`, `instrument.py`, `errors.py`)

**Interface:**

- **`Portfolio` Protocol** — three synchronous methods, `position(symbol) -> PositionView | None`, `open_positions() -> tuple[PositionView, ...]`, `account() -> AccountView`. No `strategy_id`/`venue`/`account_id`/`currency` argument (scoped at injection); no `await` point, which is what makes two reads in one handler unable to straddle a fill (ADR-0041 §7). It exists for **dependency direction**, not swappability: a `domain` strategy must not import `engine`.
- **`Exchange` gains four members** — `async start()` (validate bounds; on live: mode gate, then the leverage push), `async stop()` (cancel anything the adapter runs; paper's funding loop, live a no-op), `account_spec() -> AccountSpec` (synchronous, read once at composition, peer of `instrument_specs()`), and `async fetch_account_state() -> VenueAccountState | None` (the reconcile pull; `None` = **no venue truth to compare against**, never "flat" — ADR-0011 inv 1 in the type, exactly as `fetch_order` carries it. On live that is a failed read; on paper it is the permanent answer, because `PaperExchange` has no account truth to report at all — see the `paper` section).
- **`Store` gains five members** — `checkpoint_ledger(*, account, positions=(), order=None, funding_mark=None, ts_ns)` (one transaction across all three aggregates), `all_positions()` (the unattributed partition **unfiltered**), `load_account()`, `funding_mark(symbol)`, `has_orders()` (ADR-0043 §9).
- **Events** — `MarkTick` (`symbol`, `price`, `ts_event`; weak dedup key, conflates like `MarketTick`, carries no size or trade id); `FundingAccrual` (keyed `(account, symbol, boundary_ts)`, symbol-partitioned); a signed `fee: Decimal` on `FillReport` and `OrderFillEvent`; `VenueAccountState` + `VenuePositionState`, the frozen normalized venue read, peers of `VenueOrderView`.
- **`InstrumentSpec` gains five additive fields** — `maker_fee`, `taker_fee`, `funding_rate`, `margin_maint`, `max_leverage` — every one of them **defaulted**, so a frictionless spec stays valid and the existing construction sites keep compiling. But **not all defaulted to `0`**, and the map fixes the two that ADR-0040 §4 left unstated, because §4's field list and ADR-0044 §9's `1 ≤ leverage ≤ spec.max_leverage` bound first meet in a slice rather than in either ADR:
  - `maker_fee` / `taker_fee` (ADR-0036) and `funding_rate` (ADR-0037) default to **`0`**, exactly as those ADRs state — the frictionless-spec guarantee, not a claim about the venue.
  - `margin_maint` defaults to **`0`**: frictionless maintenance on the same pattern (ADR-0013), with the real rate authored by whoever authors the spec — the adapter from `1/(2·max_leverage)`, paper config directly (ADR-0040 §4).
  - `max_leverage` defaults to **`1`**, *not* `0`. Zero is not merely unstated but unsafe: it makes §9's bound unsatisfiable, so a default-valued spec would fault every paper start rather than validate one. `1` is the honest frictionless reading — a spec declaring no cap models no leverage — it keeps §9 satisfiable against ADR-0040 §5's own `1x`/`isolated` default, and it turns a configured `5x` against an undeclared cap into the loud refusal §9 wants instead of a silent accept.

  Making either field **required** was rejected: it would break all eleven existing `InstrumentSpec(...)` sites for a value neither the guard nor the quantizer needs, which is what "additive" is supposed to prevent. **No `margin_init`** (ADR-0040 §4).
- **Errors** — `StoreAccountMismatch`, `VenueLeverageMismatch`, `VenueAccountModeUnsupported`, all `InvariantViolation` subclasses (ADR-0014's fail-fast class, which pierces the containment net).

**Responsibilities:** the stable contract every new module compiles against; no behavior.

**Depth note:** The `Exchange` widening is where the "identical compute, different provenance" grain is enforced structurally: paper and live differ only in what these four methods return, never in what the engine does with them.

---

### PortfolioProjection (`engine/portfolio.py`)

**Interface:** `PortfolioProjection` — the write-through projection of `Position`/`Account`, the economic sibling of the order `Cache` and a **separate** projection from it (different key, different store rows, different venue anchor — ADR-0035). Callers must know:

- **Tier-1 is fill-fed synchronously**, never by subscription — the projection is the writer, so a fill is applied **exactly once** and the two read-models move atomically from any reader's view (ADR-0035, ADR-0045 §1).
- **Tier-2 is mark-fed by subscription** — the projection subscribes to `MarkTick` on the bus into a private `symbol -> (mark, ts)` latest-value map, and that asymmetry is principled: accumulated state is ordering-critical, a latest-value cache is not (ADR-0039). No memoization; every read recomputes.
- **`recover()` is `check → seed-genesis-if-absent (paper) → restore`**, called by the runner *before any other recovery work* — before `cache.rebuild()`, because it can refuse the store outright and asks only `has_orders()` rather than the mass read (ADR-0043 §10).
- **A funding accrual is gated by the durable per-symbol watermark before it is split across positions** — at or below the mark it is dropped, above it is applied and advances the mark **inside the same transaction** as the funding line it guards (ADR-0043 §5.2).
- **`for_strategy(strategy_id) -> Portfolio`** returns the scoped facade the composition root injects. The facade is bound to a real `strategy_id`, so the `None` partition is structurally unreachable through it (ADR-0041 §5).
- The concrete carries a **wider read surface than the seam** — every partition including `None`, the account-net per symbol, the foreign-flow signal — for the reconciler, telemetry and the CLI, which read the concrete and never the `domain` Protocol (ADR-0041 §8).

**Responsibilities:** partitioning fills across `(strategy, symbol)`; the account-net aggregation that is the reconciliation anchor and the position-grain denominator; the mark cache; funding application and its watermark gate; Tier-2 read assembly (delegating the arithmetic to `valuation.py`); ledger recovery; the scoped facade; the `position.*`/`account.*` named events.

**Seams:** Implements no seam itself; the `ScopedPortfolio` facade it hands out implements `Portfolio`. Consumes `Store` and `EventBus`.

**Depth note:** The economic twin of the `Cache`'s deletion argument. Without it, "what do I hold and what is it worth" is answered by the `ExecutionManager` (for writes), the reconciler (for heals), each strategy (for reads) and the store (for recovery), each with its own notion of *now* — and the Σ-invariant has no owner. It is also the only place the two-tier split is visible as one shape: the same object is written store-first on one path and subscribed on the other, on purpose.

---

### LedgerReconciliation (`engine/ledger_reconcile.py`)

**Interface:** The account-grain healing cycle, constructed with the `Exchange`, the `PortfolioProjection`, the `Clock`, the `EventBus` and a config carrying the ADR-0040 §6 band. Engine-internal, not a Protocol. **Live-only** — ADR-0034 places the heal "on live" because paper has no venue to heal *from*, in the strict sense: `PaperExchange` "persists nothing and holds no position state" (ADR-0043 §4), so there is no second account for a paper cadence to compare the projection against. Nor may one be manufactured — a paper exchange that tracked positions off its own fills would be the "second internal projection that would apply identical fills to an identical position and only ever agree with itself" ADR-0035 rejects as the Σ-invariant's check. The one real divergence paper can suffer, a crash between the fill and its checkpoint, is closed by the atomic ledger write instead (ADR-0043 §4). The runner wires the cadence on the live path alone. Callers (the runner) must know:

- The anchor is **one `fetch_account_state()` read per cycle**; `None` **freezes** the cycle and heals nothing — an outage is never a flat book (ADR-0011 inv 1, ADR-0034).
- **Tier-1 divergence heals through synthetic events** on the same idempotent `apply()` path as everything else — a reconciliation fill and/or a cash adjustment, deterministically keyed and `reconciliation`-flagged — never a blind field overwrite, so every heal leaves a "why did it move" record.
- **Tier-2 divergence only alerts**, inside `max(atol, rtol × reference)` where the reference is **the notional the quantity's mark-sensitivity flows through**, never the compared value (ADR-0046 §5). Two suppressions: a Tier-1 heal this cycle for that symbol, and a stale or absent mark.
- **Before any account-cash heal it re-reads the venue account mode.** A changed *or unverifiable* mode refuses the heal, freezes the account-grain reconcile and emits `ACCOUNT_MODE_UNVERIFIED` — a **freeze, never a fault**, because the local ledger is still correct and only the cross-check has stopped (ADR-0046 §4).
- **Per-strategy attribution is never reconciled** — the venue has no per-strategy truth. Only the account net is, and the residual lands in the `None` partition, which is what makes the Σ-invariant hold by construction (ADR-0034/0038).
- A funding correction re-enters as a keyed `FundingAccrual`, **never** as a column fix on the heal, or the watermark falls behind the sum it guards (ADR-0043 §9).
- Post-boot leverage drift is a **direct exact-match check** against config emitting `LEVERAGE_DIVERGENCE`, never a re-push (ADR-0044 §10).

**Responsibilities:** divergence classification by tier; synthetic-event construction; the three alert types; the mode-verification gate; the freeze discipline.

**Seams:** Consumes `Exchange.fetch_account_state()` — query-shaped, never a bus message (ADR-0004).

**Depth note:** The correctness net for the money line, and separate from `Reconciliation` for a reason the interface makes obvious: every rule above is about an account snapshot, a mode, and a tolerance band, none of which the cloid-anchored order cycle has any use for. Folded together, one class would carry two anchors, two freeze grains and four alert types — and the mode gate, which must run *before* a write the order reconciler never makes.

---

### Engine extensions (`execution.py`, `cache.py`, `strategy_host.py`, `runner.py`)

**Interface:**

- **`ExecutionManager`** — on a **fill** transition it makes **one** `Store.checkpoint_ledger(order=…, positions=…, account=…, ts_ns=…)` call spanning the order row and the ledger rows, then projects both read-models in memory; store-first stays the rule, and the atomicity is what makes the paper path survive a crash at all (paper has no venue to heal from — ADR-0043 §4). Every **non-fill** transition keeps `Cache.checkpoint` unchanged, none of them touching the ledger. `Cache` therefore gains an in-memory-only `project(order, ts_ns)` for the atomic path; a checkpoint the store cannot make durable stays `InvariantViolation`.
- **`StrategyHost`** — two new fail-fasts at registration: a second strategy declaring a symbol another already owns (ADR-0034's disjointness rule, unenforced today), and the reserved literal `__unattributed__` as a `strategy_id` (ADR-0043 §2, mirrored by a `StrategyConfig` validator).
- **`Engine`** — the startup sequence gains `PortfolioProjection.recover()` immediately after `bind_run_id` and before `cache.rebuild()`; `Exchange.start()` at step 4, **before** the barrier, so both venue refusals precede any order and the barrier observes an aligned venue; the barrier gains the **live-only** account materialisation under its existing failure policy (bounded retry → `FAULTED`, never a cleared barrier with no account row); the **live-only** ledger cadence joins the reconcile cadences; `exchange.stop()` joins `_teardown_steps` after `feed.stop`, so the funding generator stops before the bus drains.

**Responsibilities:** unchanged in kind — the manager still owns the checkpoint step, the host still owns registration and containment, the runner still owns ordering.

**Depth note:** The runner's docstring rule holds: lifecycle knowledge lives in one ordered sequence, not smeared across the components it starts. Three refusals (`StoreAccountMismatch`, `VenueAccountModeUnsupported`, `VenueLeverageMismatch`) now fire from that sequence in a fixed order — local store first, then venue mode, then venue leverage — and each one names every disagreeing field or symbol at once rather than one per restart.

---

### paper (`adapters/paper/`)

**Interface:** `PaperExchange` gains: a signed `fee` stamped on every emitted fill, computed from `InstrumentSpec` maker/taker rates via the `economics.py` helper, with maker/taker decided at the fill boundary where the exchange already knows it (taker iff the fill happens on arrival; maker iff it comes off the resting book) and **not** stored on the event; `account_spec()` returning the `paper-<label>` qualified id, `NET` netting and the operator's `genesis_collateral`; `fetch_account_state()` returning **`None`, always — by construction, not by failure**. Paper has no account truth to answer from: it holds resting orders, per-cloid fill reports and the latest tick, and no position, cash or equity state at all (ADR-0043 §4). `None` is the only return that stays **fail-closed under every wiring**, including a future one that mistakenly points the cadence at paper — it freezes and heals nothing (ADR-0011 inv 1), where a zero-filled `VenueAccountState` is fail-*open*: precisely the fabricated flat ADR-0034 forbids, and it would heal a restored ledger to flat. This is not the outage sentinel abused; it is the same contract — *no truth to compare against ⇒ never heal* — reached by a different route. The reconciler's own tests read recorded venue responses through `HyperliquidExchange`, per the PRD's mock boundary, never `PaperExchange`. `start()` performing the ADR-0044 §9 bounds validation (identically to live) and spawning the funding loop; `stop()` cancelling it. `adapters/paper/funding.py` holds the generator: it settles **every boundary strictly crossed** since the last — funding is additive, not convergent, so a virtual-time jump across N boundaries accrues N payments — built on `Clock.sleep_until`, deliberately *not* on `run_cadence`'s collapse-to-one semantics (ADR-0037). It **skips the restart gap**: the loop resumes from the startup instant, never from the watermark (ADR-0043 §5.1). Paper's price for the funding notional is its one price signal, the last trade.

**Responsibilities:** the venue's half of the economics on the deterministic path — fee arithmetic, funding generation, the static account declaration (`account_spec()`, carrying the operator's genesis), boot-time bounds validation. Account *truth* is deliberately not among them.

**Seams:** Adapter of `Exchange`. The `FillModel` seam is untouched and still emits price + quantity only.

**Depth note:** Paper is the reason `Exchange` is the fee and funding seam at all: the two implementations the ADR-0032 bar demands already exist as the two adapters, one computing and one ingesting. Keeping the generator in its own module keeps a running task and its cancellation out of a class whose other 200 lines are a synchronous matching book.

---

### feed (`adapters/feed/replay.py`)

**Interface:** `ReplayFeed` derives a `MarkTick` per row from the trade price and publishes it alongside the `MarketTick` — **no row-schema change**, because replay is a paper deployment reading the same trades-only JSONL (ADR-0039). The feed always emits a uniform `MarkTick`, so the projection consumes one identical stream on every deployment and is provenance-agnostic. `MarkTick` never conflates on `ReplayFeed` (replay must stay faithful).

**Responsibilities:** mark synthesis at the ingress boundary — the normalize-at-adapter half of "one provenance per deployment, no runtime blending".

**Seams:** Adapter of `MarketFeed`.

**Depth note:** One provenance rule in one place is what keeps `PortfolioProjection` free of an `if live:` for the mark. The alternative — the projection resolving "current mark" from `MarkTick` on live and `MarketTick` on paper — would make its subscription set differ per deployment.

---

### store (`adapters/store/`)

**Interface:** Three additive tables applied by the existing `CREATE TABLE IF NOT EXISTS` DDL — `positions` (PK `(strategy_id, symbol)`, `strategy_id` `NOT NULL` with `__unattributed__` as the sentinel, nullable `entry_price` and `isolated_collateral`), `account` (single row by `CHECK (id = 1)`, `genesis_collateral` `NOT NULL` with **no** `CHECK`, plus a write-once `genesis_ts_ns`), and `funding_marks` (one row per traded symbol). Callers must know: `checkpoint_ledger` is **one transaction** across every aggregate it is handed; every money column is `TEXT` round-tripped through `Decimal` on both backends, exact in *representation* (trailing zeros, `-0`, exponent forms all survive); the `_records.py` mapping is shared so the two backends cannot drift on what a row is; `account_id` is stamped on the single account row and **nowhere else**. No schema-version table and no migration framework — the change is purely additive, and the first non-additive one will need per-backend handling.

**Responsibilities:** the ledger schema, the atomic write, the four recovery reads.

**Seams:** `Store` — two real adapters, held to one behavior by the existing contract suite, which grows ledger cases rather than a new mechanism.

**Depth note:** The atomic write is the whole crash-safety argument for the money line, and it is one tested code path per backend. The `NULL`-vs-sentinel call is where the parity promise would otherwise break silently: a `NULL` partition key duplicates rows on SQLite (upsert inserts) and is rejected outright on Postgres — differently broken on each, and the silent one is the default path.

---

### hyperliquid account (`venues/hyperliquid/account.py`)

**Interface:** The one place `clearinghouseState` becomes `domain`. Callers must know: **equity is `marginSummary.accountValue`**; **free margin is `crossMarginSummary.accountValue − crossMarginSummary.totalMarginUsed`**, and the root `withdrawable` is **not read at all** — it additionally deducts margin reserved by resting orders, which this surface does not model and ADR-0024 leaves on the venue across a graceful stop, so the gap is the normal state and no tolerance absorbs it (ADR-0046 §2); `crossMaintenanceMarginUsed` is **cross-only**, so it cross-checks the cross subset while the reported Σ covers every position (§2.1); an isolated position's collateral is recovered as `marginUsed − unrealizedPnl`, **never** from `rawUsd`, which is the cash leg net of cost basis and measures negative for a long; `liquidationPx` is read through verbatim and is legitimately `null` for the majority of cross longs. `account_spec()` composes the qualified `hyperliquid-<network>-<address>` id from the resolved trading address (`vault_address or account_address or wallet.address`), and declares `genesis_collateral = None`.

**Responsibilities:** every venue-field semantic in the account response, normalized into `VenueAccountState`/`VenuePositionState`; nothing else in the codebase may know a Hyperliquid field name for these quantities (ADR-0045 §3: conventions are normalized in the adapter, never in `domain`).

**Seams:** Feeds the `Exchange` seam's `fetch_account_state()` and `account_spec()`.

**Depth note:** This module is where two rounds of testnet measurement are cashed in. Every one of the six corrections [#153](https://github.com/MarcosACH/tickwright/issues/153) landed is a statement about a field read here, and concentrating them means a seventh correction is a one-file change rather than a hunt.

---

### hyperliquid preflight (`venues/hyperliquid/preflight.py`)

**Interface:** The two boot guards, in order. **The account-mode gate** runs first and gates everything after it: one unsigned `userAbstraction` read, an **allowlist** of exactly `{"default", "disabled"}` (both are Manual/Standard; the prescribed remediation produces `"disabled"`, so a `== "default"` check would refuse the very account the operator was told to build), and `VenueAccountModeUnsupported` on anything else — including an **unreadable** mode, which is bounded-retried under the barrier budget and then faults, never assumed good. The error is a remediation: it names the observed mode, both accepted literals, and the `userSetAbstraction("disabled")` + spot→perps `usdClassTransfer` fix, both **user-signed** actions an agent wallet provably cannot perform. **The leverage push** then splits each strategy-traded symbol three ways off one `clearinghouseState` read — aligned position → skip, no position → write blind, held position that disagrees → `VenueLeverageMismatch` naming every disagreeing symbol. Pushed **once, at boot, never again**: an operator who lowers leverage in the venue UI to de-risk a live position must not be silently reverted.

**Responsibilities:** the mode allowlist and its remediation text; the three-way push split; `updateLeverage` and nothing else.

**Seams:** Called from `HyperliquidExchange.start()`.

**Depth note:** Both guards are refusals that must fire before the barrier and before any order, and both fail **closed**. Isolating them makes that testable against recorded responses without touching the order path, and keeps the one signed write in this whole map in a module a reader can audit in full.

---

### hyperliquid funding + feed (`venues/hyperliquid/funding.py`, `feed.py`)

**Interface:** `funding.py` ingests `userFundings` (WS) / `userFunding` (REST) and emits `FundingAccrual` with `amount` taken **verbatim** from `usdc` — zero sign transformation, so reconcile is a direct field compare and there is no flip bug — keyed by the venue's `time`. It **sorts each delivered batch by `time` ascending before applying**, because the venue documents no delivery order and a gate fed an out-of-order batch silently drops real payments; across deliveries the documented behavior (snapshot then hourly stream; forward-paginated history) supplies the monotonicity the watermark needs. `feed.py` subscribes the public per-coin `activeAssetCtx` channel and emits a `MarkTick` per update from `ctx.markPx` — unauthenticated, so the feed stays keyless (ADR-0021); `allMids` is mids, not mark, and stays rejected.

**Responsibilities:** funding ingest and ordering; mark ingress on the live path.

**Seams:** Adapters of `Exchange` and `MarketFeed` respectively.

**Depth note:** The batch sort is the module's real content: the durable watermark is only as safe as the ordering it is fed, and this is the one place that ordering can be established. It belongs in the venue's own tests against a recorded out-of-order batch, not in the store contract suite.

---

### strategies (`strategies/single_shot.py`)

**Interface:** `SingleShotMarketStrategy` gains a keyword-only `portfolio: Portfolio` constructor parameter, beside the `bus`/`clock` pair it already takes. **The read it makes is the tracer's assertion**, and it goes in the handler that already keeps the strategy's observable record: `on_order_event` appends the `OrderFilled` to its public `fills` list, and now also calls `portfolio.position(event.symbol)` and records the returned `PositionView` on a public `positions` list. Same shape as `fills`, deliberately — the slice's end-to-end test then asserts the traced position through the strategy's own surface rather than reaching into the projection. The read is coherent by construction: the projection is the fill's **writer**, applying Tier-1 synchronously on the `ExecutionManager` fill-apply path rather than by subscription, so both read-models have already moved by the time the `OrderFilled` reaches a strategy (ADR-0035, ADR-0045 §1) — the position read is the one that fill just produced.

`SingleShotLimitStrategy` **does not take the parameter.** It reads no portfolio state, and ADR-0041 §7 is explicit that such a strategy "simply omits the arg"; adding it for symmetry would ship a constructor parameter with no reader, and `_build_strategy`'s per-arm `match` imposes no uniformity constraint that would force the pair. The seam's second consumer arrives when a strategy has a reason to read, not to fill out the tree.

Callers must know: the strategy holds the **`domain` Protocol**, never the `PortfolioProjection` concrete and never the `ScopedPortfolio` class — that is the whole point of the seam existing for dependency direction (ADR-0035, ADR-0041 §8). It arrives already scoped to this `strategy_id`, so there is no account or strategy argument to pass and the unattributed partition is unreachable through it (ADR-0041 §5). Reads are synchronous, so two reads inside one handler cannot straddle a fill (ADR-0041 §7).

**Responsibilities:** consuming the read seam. No accounting logic of its own — a strategy that recomputes a quantity `valuation.py` already assembles is the divergence ADR-0034's identical-compute grain exists to prevent.

**Seams:** Consumer of `Portfolio` (`single_shot.py` alone). Still an implementer of `Strategy`, unchanged — the seam is a constructor parameter, not a Protocol member (ADR-0041 §7).

**Depth note:** Thin by design, but it is the layer that makes the tracer slice vertical: PRD #168's Slice 1 is "a replayed fill produces a `Position` **a strategy reads** through `Portfolio`" — singular, and one reader discharges it, so the seam ships with a consumer in the same PR that introduces it rather than with none. Constructor injection rather than a `StrategyHost` lookup is what keeps `strategies` free of an `engine` import.

---

### app (`app/config.py`, `app/build.py`)

**Interface:** `AppConfig` gains **`leverage: dict[str, LeverageSpec]`** — a top-level, venue-agnostic peer of `strategies` and `engine`, never nested under `paper` or `hyperliquid`, because its consumer is venue-agnostic and no live run may read a paper block. `PaperExchangeConfig` gains `genesis_collateral: Decimal | None = None` (`gt=0` on any value present) and `account_label: str = "default"` (lowercase slug, no hyphen, so `paper-<label>` stays unambiguously two segments against live's three). The genesis demand is a **`model_validator(mode="after")` on `AppConfig`** keyed on `exchange == "paper"` — never a required field, which would fire during field validation of `paper` and force a paper number onto a live run. A `leverage` entry naming a symbol no strategy trades is rejected at load. `build.py` **resolves** the sparse leverage map against the strategy-declared symbol set into a complete map and injects *that* into both consumers, so the model and the venue cannot disagree about what an unconfigured symbol means; it constructs the projection and hands a strategy its scoped `Portfolio` the same way it already hands it a `Clock`: **`build_engine` resolves** `projection.for_strategy(strategy_config.strategy_id)` in the registration loop and passes it as `_build_strategy`'s new `portfolio=` argument, beside `bus`/`clock`; `_build_strategy` forwards it to the arms whose constructor declares it (today `single_shot_market` alone; ADR-0041 §7 lets the rest omit it). The resolution belongs to the caller, not the arm — ADR-0041 §7 puts it there ("`build_engine` constructs the projection *and* the strategies, so **it** hands each strategy a scoped `Portfolio` facade"), and it is where `clock` already comes from. The `case "paper":` arm stays the single reader of `config.paper`.

**Responsibilities:** the new config surface, the leverage resolution, facade injection.

**Seams:** None of its own.

**Depth note:** The resolution step is the only place both inputs are in scope — an `Exchange` knows nothing of strategies, and the two symbol sets an adapter *does* hold (the feed subscription list, paper's instrument universe) are both the wrong ones. Doing it here is what makes "paper and live compute off the same input" structural rather than a convention.

---

### observability (`observability/catalog.py`)

**Interface:** New `NamedEvent` members landing **one slice at a time**, each with its emitting path and a catalog-walk test: `position.opened` / `position.changed` / `position.closed` (a flip through zero emits `closed` then `opened`, because the residual opens a fresh average-cost record), `account.reconciled`, and the three alert types — `VALUATION_DIVERGENCE` (a computed number outside the band), `LEVERAGE_DIVERGENCE` (a discrete operator setting the engine declines to re-impose, exact match, no band), and `ACCOUNT_MODE_UNVERIFIED` (the cross-check *stopped*, which is why it is not a `*_DIVERGENCE`).

**Responsibilities:** the telemetry contract for the surface.

**Seams:** None.

**Depth note:** ADR-0045 §1 closes the bus catalog for this surface — a position change is an **output** derived from a fill already on the bus, so it is never a bus event — which makes the named-event catalog the *only* place a position change is observable from outside. That raises the stakes on the catalog-walk census rather than lowering them.

## Dependency graph

```
app ────────────────▶ engine, adapters/*, venues/*, strategies, domain, observability
engine/portfolio ───▶ domain (Protocols only), observability
engine/ledger_reconcile ▶ domain, observability, engine/portfolio
engine/execution ───▶ domain, observability, engine/{cache,portfolio}
adapters/paper ─────▶ domain, observability
adapters/feed ──────▶ domain, observability
adapters/store ─────▶ domain, observability
venues/hyperliquid ─▶ domain, observability
strategies ─────────▶ domain                          (Portfolio, never engine)
domain/valuation ───▶ domain/{position,account,leverage,instrument}
domain ─────────────▶ (nothing)
```

No cycles, and no new edge in the package graph — every addition lands inside a package that already had that dependency. `domain` stays stdlib-only and log-free; `strategies` reaches portfolio state through the `domain` `Portfolio` Protocol and still never imports `engine`. Enforced by the existing `import-linter` contract (ADR-0032).

One intra-`engine` edge is new and deliberate: `ledger_reconcile` and `execution` both depend on `portfolio`, and `portfolio` depends on neither. The projection is the shared write path; nothing it owns reaches back out.

## Out of scope

Modules considered and rejected, so they are not re-litigated.

- **A `FeeModel` / `FundingModel` seam** — the two implementations already exist as the two `Exchange` adapters; a paper-side seam would be single-implementation and the live side merely reads a field (ADR-0036/0037).
- **`PortfolioProjection` as a Protocol** — one implementation by decision (identical compute, ADR-0034). `Portfolio` is the seam, and it exists for dependency direction, not swappability (ADR-0035).
- **A `MarkCache` module** — the projection is the single reader and writer of Tier-2, so a private latest-value map is the whole mechanism (ADR-0039).
- **A ledger cadence on `Reconciliation`** — different anchor, different freeze grain, its own alert types, and a mode gate the order cycle has no use for (map decision, above).
- **An account-net `Position` object with its own fill application** — the net is a computed aggregation, and a second projection applying identical fills would only ever agree with itself; the independent check is the venue (ADR-0035). A distinct account-net position is the `HEDGE` extension point.
- **A ledger repository / accounting-store adapter** — the `Store` seam already has its two adapters; a third boundary over the same two backends is a seam with no second implementation.
- **A `PositionChanged` / `AccountState` bus event** — an output derived from a fill already on the bus, keyed and idempotent; it would ship with zero consumers (ADR-0045 §1). Telemetry rides the named-event catalog.
- **`margin_init` on `InstrumentSpec`** — the initial-margin fraction is `1/leverage` off configured leverage, not static metadata; a constant `1.0` field is the speculative seam the bar warns against (ADR-0040 §4).
- **Collateral currency on `AccountSpec`** — a field with one possible value. The trigger for spending the additive path is a venue or instrument class settling in anything but USDC (ADR-0042 §2).
- **A `CashAdjustment` event** — deposits/withdrawals/transfers are not modelled on either path; a real one on live is a benign, documented divergence that heals and alerts (ADR-0042 §4).
- **A ledger line-item audit log table** — named, deferred, additive whenever taken but never retroactive; the single write site ADR-0035 established is what keeps it cheap to add later (ADR-0043 §1).
- **A schema-version table / migration framework** — the change is purely additive (ADR-0043 §8).
- **`updateIsolatedMargin`** — paper cannot model it, there is nothing steady-state to push from, and live already observes its effect through the ingested collateral (ADR-0044 §8).
- **An `activeAssetData`-based idempotent leverage push** — now unblocked ([#142](https://github.com/MarcosACH/tickwright/issues/142) verified the premise) but declined: it costs a raw `Info.post` where a typed SDK call exists, to buy idempotency on writes that are already free against the address budget (ADR-0044 §4).
- **A margin-tier table module** — flat tier-0 ships; the bands and their continuity-derived deductions are both reachable from `meta.marginTables` when the extension point is taken, with a "no table ⇒ flat `1/(2·max_leverage)`" fallback for the ~84% of the universe whose `marginTableId` is unpublished (ADR-0040 §4).
- **A `RiskEngine` / margin enforcement / liquidation execution** — the surface reports and never acts; the `PreTradeGuard` stays thin (ADR-0017). A future map.
- **A telemetry/CLI read-surface module** — a named, deferred extension point on the `engine` concrete; the boundary fixed here is only that the `domain` Protocol is strategy-only (ADR-0041 §8).
