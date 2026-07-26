# Reported margin, leverage & liquidation price: cross **and** isolated, compute-everywhere except liquidation, flat maintenance margin

_Accepted via the grilling session on decision ticket [#134](https://github.com/MarcosACH/tickwright/issues/134), part of the trade-economics map [#107](https://github.com/MarcosACH/tickwright/issues/107). Fixes the numbers ADR-0034 (D1) classed as **Tier-2 recomputed valuations** and ADR-0035 (D2) placed on the `PortfolioProjection`; consumes the mark ADR-0039 (D6) put on the feed; delivers the additive `InstrumentSpec` fields the map's Notes anticipated; and **refines ADR-0038 (D5)** — cross-vs-isolated margin mode is per-symbol, not an `AccountSpec` field. Grounded in the Hyperliquid margin/leverage/liquidation math captured verbatim by R3 ([#110](https://github.com/MarcosACH/tickwright/issues/110), `docs/research/hyperliquid-margin-leverage-liquidation-mark.md`)._

The accounting surface **reports** margin, leverage and liquidation price; it never rejects an order for margin and never liquidates (enforcement is out of scope — a future map). This ADR fixes exactly which numbers exist, how each is produced on paper and on live, and how far a live-recomputed number may drift from the venue's before we alert.

## 1. Scope: cross **and** isolated, both first-class; mode and leverage are per-symbol

v1 models **both** margin modes as first-class — not cross-only with isolated deferred. Isolated is the primary mode in practice, so it is not a speculative seam.

Margin mode and leverage are a **per-symbol / per-position** attribute, **not** an account-wide one. On the venue, `updateLeverage({asset, isCross, leverage})` is per-asset and `clearinghouseState` reports `leverage.{type, value}` (plus `rawUsd` when isolated) **per position** — an account can run one symbol isolated-20x and another cross-10x simultaneously. This **refines ADR-0038**, whose `AccountSpec` note sketched margin mode as an additive *account* field: mode lives with the position, not the account. `AccountSpec` retains only the account-wide facts (`account_id`, `NET`/`HEDGE` netting, and — still additive — collateral currency).

**Paper isolated collateral is static at open.** An isolated position's collateral is the amount moved in **at open** (`notional / leverage`); paper does **not** model `updateIsolatedMargin` top-ups or withdrawals — that is an order-side account-management action, outside this reporting surface. Paper isolated liquidation price is computed off that static collateral plus accrued unrealized PnL.

## 2. The Tier-2 quantity set and its grain

The Tier-1 accumulated ledger (net size, average entry, realized PnL, fees, funding, cash) is fixed by ADR-0034/0036/0037. This ADR owns the **Tier-2 valuations** — recomputed on every read from `(position, mark)`, never stored (ADR-0034). All `Decimal` (ADR-0029).

| Quantity | Definition | Per-position | Account |
|---|---|---|---|
| `notional` | `\|szi\| × mark` | ✓ | Σ = total notional |
| `unrealized_pnl` | `szi × (mark − entry)` | ✓ | Σ |
| `equity` | `cash + Σ unrealized_pnl` | — | ✓ (the anchor) |
| `margin_used` | cross: `notional / leverage`; isolated: `isolated_collateral + unrealized_pnl` — the position's own equity (§3; the row originally read "the position's locked collateral (an ingested input)", corrected by [#142](https://github.com/MarcosACH/tickwright/issues/142)) | ✓ | Σ = total margin used |
| `maintenance_margin` | `notional × margin_maint` (flat, §4) | ✓ | Σ |
| `free_margin` | `equity − total_margin_used` | — | ✓ |
| `liquidation_price` | §3 (read-through live / computed paper) | ✓ (nullable) | — |
| `effective_leverage` | isolated position: `notional / (isolated_collateral + uPnL)`; cross position: `notional / equity`; account: `total_notional / equity` (denominator refined by ADR-0041 §4.1) | ✓ (nullable) | ✓ (nullable) |

**Grain rule (from ADR-0034 + the P1 prototype, [#119](https://github.com/MarcosACH/tickwright/issues/119)):** the per-strategy overlay carries only **`size`, `unrealized_pnl`, and realized PnL** — the numbers a fill partitions linearly (size) or attributes. **Margin, equity, free margin, liquidation price and effective leverage are account-wide (cross) or per-position (isolated) by nature — never per-strategy**: a strategy does not own collateral, so "this strategy's margin" is meaningless. How a strategy *reaches* the account-level numbers is the `Portfolio` read-API's problem ([#135](https://github.com/MarcosACH/tickwright/issues/135) — **now fixed by ADR-0041 §2**); this ADR only fixes that they exist at account/position grain.

**Two deliberate calls:**

- **`effective_leverage` is convention-only.** The venue defines no such term and exposes no field for it; it is the realized exposure-to-equity ratio by convention. Its per-position denominator is the **position's own equity for isolated** (`isolated_collateral + uPnL`) and **account equity for cross** — a modelling choice R3 §2.3 flagged as *inferred / needs confirmation* (pending #142), refined by ADR-0041 §4.1 (this ADR's original flat `notional / equity` mis-stated the isolated case). It is **nullable on a non-positive denominator** — a wiped or negative backing equity (reachable here, §7) has no meaningful exposure ratio (ADR-0041 §4.1/§6). It is exposed as a convenience but stands **outside the divergence alert band (§6)** — there is nothing venue-side to compare it to.
- **No account-level liquidation price.** The venue reports `liquidationPx` **per position** (each position's liquidation assuming the others are held); there is no single account liquidation price. Liquidation stays strictly per-position, even under cross.

## 3. Computed everywhere, except liquidation price

Every Tier-2 number is **computed on both paper and live** from `(position, fresh feed mark (ADR-0039), leverage, margin_maint)`. On live the venue-reported values (`unrealizedPnl`, `marginUsed`, `positionValue`, `accountValue`, `withdrawable`, `crossMaintenanceMarginUsed`) are the **divergence cross-check (§6), not the input** — computing keeps the numbers fresh at feed-mark cadence, per-strategy-attributable, and identical across paper and live (ADR-0034's core grain).

**Liquidation price is the one recomputed *valuation* read through the venue on live, computed on paper.** ADR-0034 flagged it as a candidate; this ADR confirms it. Three reasons it is special: (i) re-deriving it needs the maintenance-margin **tier fixed point** — self-referential, since the tier depends on position value *at the liquidation price*; (ii) it is safety-relevant; (iii) the venue already computes it exactly and reports it as one field.

- **Live:** ingest `clearinghouseState.liquidationPx` per position via the **reconcile pull** — exactly the channel ADR-0039 kept alive for this. Cached on the projection, **stale-frozen between reconciles**, **`None`** when the venue omits it (no position, or one it cannot price — ADR-0034's `None` → freeze, never a fabricated value). Authoritative, and **outside the alert band** — we read the venue's own number, so there is nothing to diverge against.
- **Paper:** compute the canonical `liq_price = price − side · margin_available / size / (1 − l · side)` (R3), where `l = margin_maint` (§4), `margin_available` is `equity − maintenance` (cross) or `(isolated_collateral + unrealized_pnl) − maintenance` (isolated), and `side = +1` long / `−1` short. `price` is the **mark** and `size` is `|szi|`. Cross recomputes off account equity each read; isolated recomputes off the position's own equity.

  **(Corrected and confirmed by [#142](https://github.com/MarcosACH/tickwright/issues/142).** The isolated `margin_available` originally read `isolated_collateral − maintenance`, omitting the unrealized-PnL term — the static-collateral reading this ADR's §3 paragraph above got wrong. Measured against the venue on a funded position, the omission is a **−21.27 price-unit error** (`52501.23` computed vs `52522.4977` reported), exactly `unrealized_pnl / size / (1 − l)`.

  With the term restored, the formula reproduces `clearinghouseState.liquidationPx` to **28 significant figures** at three independent states — two marks and one `updateIsolatedMargin` top-up — which also settles R3's open Qs #1–2: `price` is the **mark**, `size` is **`|szi|`**. The liquidation price is confirmed **invariant to the mark**, as the formula implies.

  Worth recording that **§1 of this ADR already had it right** — "paper isolated liquidation price is computed off that static collateral **plus accrued unrealized PnL**" — so the document contradicted itself, and the measurement adjudicates in §1's favour. ADR-0045's sweep did not catch this pair; it is a sixth documentation defect of exactly the kind that ADR argued the deferral existed to surface.**)**

**Isolated collateral is the one other live read — but as a position *input*, not a valuation.** An isolated position's locked collateral is ingested on live (reflecting the `updateIsolatedMargin` top-ups the surface does not model, §1) and is the static open-margin (`notional / leverage`) on paper. It is ingested exactly as the per-symbol leverage/mode are (§5) — a per-position input the valuations are computed *from*, not a recomputed valuation read back.

**(Corrected by the [#142](https://github.com/MarcosACH/tickwright/issues/142) testnet measurement, twice.** This paragraph originally read the collateral off `leverage.rawUsd` and concluded that isolated `margin_used` "equals that collateral by definition on both paths", leaving only the **cross** computation inside §6's band. Both halves are false against a live venue.

- **`rawUsd` is not the collateral.** Measured on a funded isolated position: `rawUsd = −103.731933` against a locked collateral of `25.898067`. `rawUsd` is the position's **cash leg net of its cost basis** — `isolated_collateral − entry_price · |szi|` — and is *negative* for a long. The collateral is recovered as `marginUsed − unrealized_pnl`, or equivalently `rawUsd + entry_price · |szi|`.
- **Isolated `margin_used` is mark-dependent.** The venue's `marginUsed` for an isolated position is `positionValue + rawUsd` ≡ **`isolated_collateral + unrealized_pnl`**, and it moves with the mark: `25.860067` at mark 64796 → `25.856067` at mark 64794 (Δ = the Δ in uPnL), while `rawUsd` held at exactly `−103.731933`.

So the two quantities are **distinct and differently tiered**, where this ADR had one:

| quantity | tier | source |
|---|---|---|
| `isolated_collateral` | **Tier-1** — static but for `updateIsolatedMargin`, persisted (ADR-0043 §3, which stands unchanged) | ingested live / open-margin on paper |
| `margin_used` (isolated) | **Tier-2** — recomputed each read as `isolated_collateral + unrealized_pnl` | computed both paths |

Consequently **isolated `margin_used` sits *inside* §6's alert band**, exactly like cross — it is a recomputed valuation with a venue counterpart, not the same ingested number on both sides of the comparison.**)**

The accepted cost: liquidation price is the **only recomputed *valuation*** with a compute/read split (isolated collateral is read on live too, but as a position input — above), and on live it is only as fresh as the reconcile cadence. Both are acceptable because liquidation is alert-only and never enforced here, and for a fixed position it barely moves between reconciles. Computing it on live to stay pure to ADR-0034's identical-compute grain would mean owning the fixed point *and* the cross-account coupling for a number we would then merely alert on.

## 4. Flat maintenance margin; the additive `InstrumentSpec` fields

**Maintenance margin is flat (tier-0):** `maintenance_margin = notional × margin_maint`, with `margin_maint = 1/(2·max_leverage)` at the base tier and no per-tier deduction. The **piecewise margin-tier table** (`notional × mmr − deduction` above per-asset notional bands) is **deferred** as a named extension point, because:

- read-through liquidation on live (§3) removes any need for the tier **fixed point** on live;
- paper positions essentially never cross tier-0 (a >$3M position on a low-cap asset, >$150M on BTC);
- the tier table's contents (bands, deductions) are **not** in the captured API surface at the pinned SDK — only `marginTableId` is — so sourcing them means hardcoding docs values (brittle) or an unverified endpoint.

**(Two of those three reasons corrected by [#142](https://github.com/MarcosACH/tickwright/issues/142); the decision stands.** The second and third bullets are both wrong on the facts, in the same direction — they make tier-0 look far safer than it is.

- **The table's contents *are* reachable, and need no unverified endpoint.** The raw `meta` response carries a `marginTables` array; only the pinned SDK's *typed* model omits it. BTC is `marginTableId: 54` ("tiered 40x"), read verbatim from testnet:

  | lower bound | max leverage | implied `margin_maint` |
  |---|---|---|
  | $0 | 40x | 1.25 % |
  | $10,000 | 25x | 2 % |
  | $50,000 | 10x | 5 % |

- **The first band ends at $10k, not $150M** — the ">$150M on BTC" estimate is off by four orders of magnitude. It appears to have been derived from the max-leverage figure rather than read from the bands. A paper strategy can plausibly reach $10k of notional, so the extension point is far nearer than this section assumed.

The deferral itself is **not** reversed: read-through liquidation still removes the fixed point on live (bullet one, untouched), and flat tier-0 stays exact below the first band. But its stated safety margin was fictitious, and the consequence below — that a tier-crossing position trips the §6 alert — is now a *reachable* operating condition rather than a theoretical one. `InstrumentSpec.margin_table_id` remains the named extension point, with `meta.marginTables` confirmed as its source.**)**

**Consequence, stated:** flat maintenance margin slightly *under*-reports maintenance (and thus liquidation distance) for a tier-crossing position. On live that makes the *computed* maintenance diverge from the venue's `crossMaintenanceMarginUsed` and **trips the §6 alert** — the correct signal that an unmodeled regime was reached. The extension point is `InstrumentSpec.margin_table_id` plus a tier structure, reached only if a paper strategy trades tier-crossing size.

**Additive `InstrumentSpec` fields (this ADR's concrete deliverable):**

- **`max_leverage: int`** — the venue leverage cap (from `meta.universe[].maxLeverage`). Bounds/validates the per-symbol configured leverage on paper; a real venue fact.
- **`margin_maint: Decimal`** — the maintenance-margin **fraction**, flat tier-0. Carried as **explicit data, not derived**: the Hyperliquid adapter sets it to `1/(2·max_leverage)` when building the spec; paper config sets it directly. This keeps the `domain` maintenance helper venue-agnostic (`maintenance = notional × margin_maint`) instead of leaking the venue's "half the initial margin at max leverage" rule into `domain` — the same choice ADR-0036 made carrying `maker_fee`/`taker_fee` explicitly rather than re-deriving a fee tier.

**No `margin_init` field.** The initial-margin fraction is `1/leverage` off the **per-symbol configured leverage** (§5), not static instrument metadata; there is no additional per-venue initial-margin haircut, so the amount is `notional / leverage` directly. A constant `margin_init = 1.0` field would be exactly the speculative seam the ≤2-implementations bar warns against.

## 5. Leverage and margin mode: config-authoritative on both paths

The **per-symbol leverage + margin mode is the single source of truth for the margin model on both paper and live.** It is a per-symbol block carrying mode + integer leverage — **not** on `InstrumentSpec`, which stays the identical venue-metadata shape across paths (ADR-0031). Defaults are the safest pair: **leverage `1x`, mode `isolated`** (`1x` isolated = full-notional collateral per position, minimal liquidation exposure); leverage is thus off-by-default and opted into per symbol.

**(Amended by ADR-0044 §2 — the block's home.** This ADR placed it in `PaperExchangeConfig`. That is wrong for a reason independent of ADR-0044's push: the component that consumes it, `PortfolioProjection`, is venue-agnostic and needs it on **both** paths, and no live run may read `config.paper` (ADR-0042 §1). It is a venue-agnostic **`AppConfig.leverage: dict[str, LeverageSpec]`**, `LeverageSpec` a frozen `domain` value carrying the `(mode, leverage)` pair `updateLeverage` itself carries. Named `leverage`, not `margin`, because CONTEXT.md binds **Margin** to the reported collateral a position ties up (**ADR-0045 §9.5** — this once read "the computed collateral", which that sweep took to be the cross-only half; [#142](https://github.com/MarcosACH/tickwright/issues/142) withdrew that reading — `margin_used` is computed in *both* modes — so "reported" now stands as the mode-neutral word rather than as a correction. The naming argument turns on *output vs operator input* and is unaffected either way).**)**

- **Paper:** the configured values drive the model directly.
- **Live:** the model still computes from config; the ingested venue `leverage.{type, value}` is a **cross-check** — a disagreement (a failed push, or a hand-edit in the venue UI) surfaces through the resulting `margin_used` divergence (§6). The isolated position's collateral is ingested from the venue as a position input (§3), not derived from config.
  **(Corrected by ADR-0044 §10, whose reasoning is in turn corrected by [#142](https://github.com/MarcosACH/tickwright/issues/142) — the conclusion is unchanged.** The `margin_used` route is **blind for isolated positions**, so the indirect cross-check misses this ADR's own default and primary mode, and ADR-0044 adds a **direct exact-match check** of the ingested `leverage.{type, value}` against config on each reconcile, on its own alert-only event.

  The original reason given — that §3 makes isolated `margin_used` ≡ the ingested `rawUsd`, "the same number on both sides" — is false: `rawUsd` is not the collateral, and isolated `margin_used` is mark-dependent and now sits *inside* §6's band (see the §3 and §6 corrections). The real reason the route is blind is that **a leverage change never re-margins an open position** — measured across 5x → 10x → 3x with the **mark-invariant** `rawUsd` and `liquidationPx` unmoved (`marginUsed` held too, but at a fixed mark, so it corroborates rather than carries the point — ADR-0044 §5) — so neither term of `isolated_collateral + unrealized_pnl` responds to the *setting* this check is trying to detect.**)**

**The engine does not set leverage or mode on the venue as part of this surface.** Pushing config to the venue via `updateLeverage` is a signed on-chain write with its own design questions (when to apply, existing-position handling, failure/retry, and whether to also expose `updateIsolatedMargin`) and belongs to the **`Exchange` adapter**, not the reporting surface. It is captured as a separate decision ticket blocked by this one. **(Superseded by [ADR-0044](./0044-venue-leverage-and-margin-mode-write.md), as anticipated: the engine now pushes config to the venue **once, at boot**, via `Exchange.start()` — skipping symbols already aligned, writing blind where no position is held, and **refusing to start** where config disagrees with a symbol that does hold one. `updateIsolatedMargin` stays out, per §1 here.**)**

## 6. The Tier-2 divergence alert band

ADR-0034 deferred the numeric tolerance to this ticket. It applies to the mark-dependent, venue-comparable numbers — **`unrealized_pnl`, `notional`, `equity`, `margin_used` (**both** computations — see the §3 correction: isolated `margin_used` is `isolated_collateral + unrealized_pnl`, mark-dependent, not an ingested constant), `maintenance_margin` (account-level), `free_margin`** — and **not** to `liquidation_price` (read-through, no computed cross-check) or `effective_leverage` (no venue field).

- **Shape — combined absolute + relative:** alert iff `|computed − venue| > max(atol, rtol × |venue|)`, one uniform policy applied per number. A pure absolute band is useless at scale; a pure relative band screams on near-zero positions.
- **Defaults — `rtol = 0.1%` (`0.001`), `atol = $0.01`, both config.** These are *starting* values; the real tuning wants a **funded testnet position** to measure actual divergence, captured as a `wayfinder:task` graduated from this ticket. This ADR fixes the band's shape and defaults; the task hardens the constants.

  **(Measured by [#142](https://github.com/MarcosACH/tickwright/issues/142). `rtol` confirmed; the band's *shape* has a defect, carried by [#148](https://github.com/MarcosACH/tickwright/issues/148).**

  Every formula in this ADR reproduces the venue **exactly** when fed the venue's own mark — **below the first margin-tier band**, which is where the measurement sat (a ~$130 notional against §4's newly-read $10k bound) and where flat tier-0 *is* the venue's own maintenance rate. Inside that regime the band absorbs **mark skew** only — the gap between our latest `MarkTick` (ADR-0039, `activeAssetCtx`, ~3 s) and the venue's instantaneous mark at reconcile. Over 162 samples of BTC testnet: 3 s p99 `1.4e-04`, 60 s max `3.9e-04`, with a hard floor at the BTC mark quantum of $1 ≈ `1.5e-05`. Above the first band the flat-tier approximation diverges by whatever the tier table dictates, not by mark skew — that divergence is the alert §4's "Consequence, stated" says it should be, and is deliberately **not** an input to sizing `rtol` (sizing the band to absorb it would suppress the signal).

  **`rtol = 0.001` therefore stands on evidence** — ≈7× the 3-second p99, ≈2.6× the 60-second max, leaving about an order of magnitude of headroom for a fast tape (the sampled window was quiet: `6e-04` total range over 5 minutes).

  **`atol = $0.01` is not safe as specified**, because the shape above scales the relative term by *the compared value*: as a venue value approaches zero the test degenerates to `atol`, while the error does not shrink with it — the error is driven by **notional**. Two cases, the second observed directly: `unrealized_pnl` crossing zero on a 1 BTC position moves **$1.00** per mark tick against a `$0.01` band; and the cross snapshot read `withdrawable = 0.0096` against equity `25.93`, where a `1.4e-04` skew moves `free_margin` by ≈`0.018` — a false `VALUATION_DIVERGENCE` on a healthy account. The fix is to scale the relative term by a **stable reference** (`notional` for `unrealized_pnl`, `equity` for `free_margin`) rather than by the compared value, leaving `atol` as a pure rounding floor. Deliberately **not applied here**: `free_margin` and `equity` are account-grain, and #148 may move where they are sourced.**)**
- **Surfacing — one channel, distinct type:** the same alert sink as the Tier-1 heal-alert, emitting a distinct **`VALUATION_DIVERGENCE`** (alert-only, never heals) versus Tier-1's ledger divergence (heal + alert).
- **Suppression — two rules:** (1) if **Tier-1 diverged** for that symbol/account this cycle, suppress the Tier-2 alert (the healed ledger is the root cause; every valuation on it is trivially wrong); (2) if the **mark is stale or `None`** (ADR-0039 records its `ts`), suppress or annotate rather than alert — the divergence is explained by staleness, not a bug.

## 7. Paper account genesis numbers

Paper runs the same compute as live (ADR-0034); it merely has no venue to cross-check against. Given the genesis collateral configured by [#136](https://github.com/MarcosACH/tickwright/issues/136) (that ticket owns the *config*; this owns the *formulas*) — **since settled by ADR-0042**, which makes it a strictly positive `PaperExchangeConfig` field with no default, **demanded by `AppConfig`'s validator whenever `exchange == "paper"`** (never required at field level, which would force a paper number onto a live run — ADR-0042 §1), and ingests the live path's from the venue:

- **Equity — one invariant, both paths:** `equity = cash + Σ unrealized_pnl` — the Tier-1 cash line plus Tier-2 Σ uPnL. The isolated-vs-cross bucket transfers net to zero at the account level, so the invariant holds regardless of mode. **The cash line accrues** from four signed inputs — `genesis_collateral`, `+ Σ realized_pnl`, `− Σ fees`, `+ Σ funding` (ADR-0042 §4; the fee term **subtracts**, ADR-0036 fixing `fee > 0` as a cost debited and `fee < 0` as a maker rebate credited, so a fee accumulated verbatim from the venue enters the cash line negatively, while realized PnL and ADR-0037's funding `amount` are already signed deltas and add). (**Corrected by ADR-0045 §5:** this section previously attached "one invariant, both paths" to the *expanded* form `equity = genesis_collateral + Σ realized_pnl − Σ fees + Σ funding + Σ unrealized_pnl`. That expression holds only while `cash` equals the sum of its four accruing inputs, which ADR-0042 §4 says it need not on live — the reconciler's synthetic cash adjustment corrects `cash` toward venue truth "precisely because the four inputs above failed to reproduce the venue's number", so the expanded form is false the instant a heal fires on the very path the claim named. The compact form survives a heal, matches the §2 table's computation, and matches ADR-0041 §4's `AccountView`, which exposes `cash` and `equity` as separate fields. The four-input rule is unchanged — it is how `cash` **moves**, not what `equity` **is**. No number changes.)
- **Free margin:** `free_margin = equity − total_margin_used` (the cross available pool); **isolated collateral buckets are locked and excluded from free**. Before the first trade, `equity = free_margin = genesis`, positions empty.
- **Negative free margin is reported as-is — no rejection, no liquidation, no alert.** This is the map's scope ceiling made concrete, and a **deliberate departure** from a typical simulator (which rejects the margin-breaching order): ours never does. Free margin simply goes negative and is reported — the honest "you would have been rejected or liquidated on live" signal. A negative value is a **valid state, not a divergence**, so it never trips the §6 band. (This once read "a zero genesis drives it negative on the first trade; paper still runs" — ADR-0042's `gt=0` makes a zero genesis unreachable through config, but changes nothing about the rule: free margin goes negative from ordinary trading, and is reported without consequence.)

## Consequences

- **Additive only** — `InstrumentSpec` gains `max_leverage` and `margin_maint`; no field is removed, no seam is broken. `PaperExchangeConfig` gains a per-symbol leverage/mode block. **(Amended by ADR-0044 §2:** that block is **not** a `PaperExchangeConfig` field — it is venue-agnostic **`AppConfig.leverage: dict[str, LeverageSpec]`**, because its consumer `PortfolioProjection` needs it on both paths and no live run may read `config.paper` (ADR-0042 §1). Still additive; only the home changed, as §5 above records.**)**
- **Refines ADR-0038** — margin mode is per-symbol, not an `AccountSpec` field. ADR-0038's `AccountSpec` note is corrected accordingly.
- **Corrects ADR-0035** — its additive-metadata list dropped `margin_init` (§4); the list is updated in-place (docs-sync). The parent map [#107](https://github.com/MarcosACH/tickwright/issues/107)'s Notes name it too and should read `maker_fee`/`taker_fee`/`margin_maint`/`max_leverage`.
- **Confirms ADR-0034's liquidation exception** and fixes its deferred Tier-2 alert band.
- **Graduates two tickets:** an `Exchange`-side decision on applying per-symbol leverage/mode to the venue (`updateLeverage`), blocked by this ticket — **since settled by [ADR-0044](./0044-venue-leverage-and-margin-mode-write.md)**, which also relocates §5's config block and corrects its `margin_used`-divergence claim; and a `wayfinder:task` to validate the reported margin/liquidation math against a funded testnet position and tune the §6 constants (clearing the map's standing fog patch).
- **Deferred, named extension points:** the margin-tier table (`margin_table_id`), isolated-margin top-up/withdraw modeling, and multi-currency collateral.
