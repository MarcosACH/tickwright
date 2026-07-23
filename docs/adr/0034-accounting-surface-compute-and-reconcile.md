# The accounting surface computes everywhere and reconciles against venue truth on live

_Accepted via the D1 grilling session on decision ticket [#111](https://github.com/MarcosACH/tickwright/issues/111), part of the trade-economics map [#107](https://github.com/MarcosACH/tickwright/issues/107). Downstream map tickets, the PRD, and implementation build on it._

The account/position/PnL surface is a **fill-fed write-through projection** with **identical compute logic on paper and live** — it subscribes to fills like the order `Cache` (ADR-0009). On live, a reconciliation loop **heals it against the venue's account/position snapshot**, mirroring order-saga reconciliation (ADR-0011). We reject "mirror venue truth on live" (a second code path that the paper exchange — which has no venue to mirror — cannot share) and "hybrid snapshot-replace" (a coarse special case of reconciliation). One truth model, one recovery story for the whole engine.

## Divergence policy: venue-authoritative, healed via synthetic events

On live the venue is authoritative. When a reconcile cycle (at startup + on a cadence) finds a divergence, the reconciler **emits a synthetic adjustment event** — a reconciliation fill and/or a cash adjustment — carrying a **deterministic id + `reconciliation` flag**, consumed by the *same* `apply()` path (ADR-0011 invariant 6). Healing *through* an event keeps one write path, keeps every derived number internally consistent, and leaves an auditable "why did it move" record — never a blind field overwrite. **Connectivity guard (non-negotiable, ADR-0011 invariant 1):** a failed venue read returns `None`, the cycle **freezes**, and a missing position is never read as flat.

## Two-tier tolerance

- **Tier 1 — the accumulated ledger** (net position size, average entry, realized PnL, cumulative fees, funding, cash/collateral): **exact at the venue's reported precision, zero economic tolerance.** These *accumulate*, so any divergence compounds into every future number — it is a missed/duplicated fill or an accounting bug, not noise. Response: **heal via synthetic event *and* alert.** ("Exact" = exact once both sides are quantized to the venue's size/price precision, not raw float.)
- **Tier 2 — derived valuations** (unrealized PnL, equity, margin used, effective leverage, liquidation price): **recomputed on every read** from `(current position, current mark)` — never stored, so they cannot drift into future state. They will *never* be float-exact against the venue (the venue's mark is a robust median we do not replicate bit-for-bit, and it rounds per its own rules), so a **tolerance band exists only to decide whether a divergence is worth *alerting* on** (a wrong mark source or formula bug) — never to heal state.

The venue's **mark price is ingested as market data (an input)**; the derived metrics are **computed (the output)**. Feeding the pipeline the venue's mark makes live-recomputed valuations ≈ venue-reported to rounding, so we get near-zero divergence *and* a single fresh, always-available, per-strategy-attributable read path (ADR-0004: reads are method calls, never blocking venue calls). The numeric alert band is deferred to the margin/mark tickets.

## Field ownership and two-level topology

- **Account-level net aggregate** — all fills applied to one net position, mirroring the venue's netting. Same average-cost algorithm on the same fills ⇒ it matches the venue's `szi`/`entryPx`/realized exactly, and its recomputed uPnL matches (same mark). **This is the sole reconciliation anchor.**
- **Per-strategy positions** — a *different partition* of the same fills for the strategy read API. **Never reconciled against the venue** (the venue has no per-strategy truth), bridged to the anchor by one invariant: **Σ(per-strategy signed size per symbol) = account net size = venue `szi`**. Position *size* is linear and sums; PnL and its realized/unrealized split are *not* linear under netting and must not be reconciled per-strategy.
- **Liquidation price** is computed from the reported-margin formula on paper; **reading the venue's value directly on live is a candidate exception** (it is safety-relevant and its exact re-derivation needs a maintenance-margin tier fixed point) — deferred to the margin/liq ticket.

## Netting semantics gate the ownership rule (venue-agnostic)

The surface is built on a **canonical, venue-agnostic economic model**; each venue adapter **declares its netting semantics** (`NET` vs `HEDGE`) and maps its reported fields into that model. The disjointness rule is a *consequence of `NET` semantics*, not a law:

> On a `NET` (one-way) venue, `(strategy, symbol)` ownership is **disjoint per account** — the strategy registry (ADR-0018 uniqueness gate; ADR-0024 per-strategy symbol filtering) **fail-fast rejects** a second strategy declaring a symbol another already owns on the same account. Same-symbol isolation requires a **separate account** (the venue-native primitive). `HEDGE` venues may relax this.

This is because a one-way venue nets same-symbol strategies into one real position — engine-side per-strategy books keep the *accounting* consistent but cannot make two same-symbol strategies behave *independently* (one strategy's close silently changes the other's real exposure; liquidation is account-wide). Enforcing disjointness makes the engine surface that truth up front, and **collapses per-strategy attribution to exact** (per-strategy = per-`(account, symbol)`), eliminating the realized/unrealized reclassification that cross-strategy netting would otherwise create. **v1 implements `NET` only** (live venue + paper); `HEDGE` is a documented additive extension point (honoring the ≤2-implementations-per-seam grain).

## Recovery: snapshot-plus-reconcile

Recovery lifts ADR-0009 from orders to accounting, on the same crash-only code path as steady-state reconcile. The durable `Store` is **system-of-record for our ledger** — the Tier-1 accumulated state *and* per-strategy attribution, which the venue snapshot cannot reconstruct. On restart: **restore the ledger snapshot from the `Store` → reconcile current position/margin against the venue → resume.** Pure snapshot-from-venue is rejected (it loses our cumulative fees/funding, realized history, and attribution); pure replay-without-reconcile is rejected (it misses fills that landed while down). The durable-store *schema/cadence* is deferred to the durability ticket.

## Funding-accrual idempotency

Each funding accrual is a **keyed idempotent ledger event** — deterministic id `(account, symbol, funding-timestamp)` (ADR-0025) — so replay-on-restart *and* a reconcile that re-ingests the same funding both converge without double-counting. Live **ingests** funding from the venue keyed by `(time, symbol)`; paper **generates** it on the `Clock` keyed the same way — one idempotent shape, both modes. Funding *mechanics* — the precise key, sign convention, boundary schedule, paper `Clock`-driven catch-up cadence, and live `userFundings` ingest — are **fixed by ADR-0037** (resolving [#117](https://github.com/MarcosACH/tickwright/issues/117)), now the canonical source; the `funding-timestamp` above is the epoch-aligned `boundary_ts` on paper and the venue's `userFunding.time` on live.

## Consequences

- **ADR-0018 needs a caveat** (docs-sync when this lands): its "multiple strategies may trade the same symbol independently, isolated by cloids" holds at the **order** level (distinct `cloid`s, routing) but not the **position-economics** level on a `NET` venue — where disjoint `(strategy, symbol)` per account is required and same-symbol isolation needs a separate account.
- **Requires multi-account** as the same-symbol isolation primitive — its own decision, [#118](https://github.com/MarcosACH/tickwright/issues/118).
- **Cross-margin residual (out of scope):** even with disjoint symbols, a cross-margin account shares collateral and has one account-wide liquidation — inherent to one account; margin enforcement/liquidation is already out of scope for this map.
- **Shapes downstream tickets:** the position/PnL state-model prototype, the fee ([#116](https://github.com/MarcosACH/tickwright/issues/116)) and funding ([#117](https://github.com/MarcosACH/tickwright/issues/117)) models, the margin/mark models, the strategy read-API (after D2 [#112](https://github.com/MarcosACH/tickwright/issues/112)), and the durability schema.
- **CONTEXT.md terminology** (Position, Account, Portfolio, Equity, realized/unrealized PnL, Margin, Funding, Fee, Mark price, Collateral) is deferred to the terminology work, avoiding the reserved `Cache`/`Ledger` collisions.
