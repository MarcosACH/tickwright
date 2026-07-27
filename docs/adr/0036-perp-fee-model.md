# Perp fees: a signed `Decimal` fee computed at the fill boundary, carried on the fill, accrued in the surface

_Accepted via the D3 grilling session on decision ticket [#116](https://github.com/MarcosACH/tickwright/issues/116), part of the trade-economics map [#107](https://github.com/MarcosACH/tickwright/issues/107). Grounded in R2 ([#109](https://github.com/MarcosACH/tickwright/issues/109)); builds on ADR-0034 (D1) and ADR-0035 (D2). Funding ([#117](https://github.com/MarcosACH/tickwright/issues/117)) is a sibling decision, not this one._

A perpetual fill carries a **single signed `Decimal` `fee`** (negative = a maker rebate, ADR-0029 — a shorthand about the *sign convention*, qualified below: making liquidity is not what makes a fee negative), settled in **USDC**. Each `Exchange` adapter produces it at the **fill boundary** — the paper exchange **computes** it from flat maker/taker rates on `InstrumentSpec`; the live exchange **reads** the venue-reported fee — and the `PortfolioProjection` merely **accrues** it (ADR-0035). This lifts ADR-0013's fee deferral: the portfolio surface that ADR named as the precondition now exists, so the additive fee seam it anticipated is introduced here.

## The fee seam is the `Exchange` adapter, not a swappable fee-model object

The two implementations "per seam" (ADR-0032) already exist as the two `Exchange` adapters: `PaperExchange` and `HyperliquidExchange`. A fill's price, quantity, and fee are the same kind of fact — *what happened at the venue* — so the fee is another economic attribute each adapter stamps on the fill it emits, exactly as it already emits price and quantity.

- **Paper** computes `fee = notional × rate`, selecting the maker or taker rate from `InstrumentSpec` via the maker/taker decision it already makes. The arithmetic is a **pure `domain` helper** — a peer of `quantize_size` / `below_min_notional` — so it is unit-testable in isolation and shared, without being an injected strategy object.
- **Live** reads the venue-reported `fee` straight off the fill payload (R2: the venue reports the fee per fill).

A dedicated `FeeModel` seam (paralleling the paper `FillModel`, ADR-0012) was **rejected**: a flat schedule is deterministic config, not a nondeterminism model, and the seam would be **single-implementation** on the paper side (a hardcoded smell) while the live side merely reads a field — no model to speak of. That abstraction earns its place in a multi-fee-schedule backtester; Tickwright v1 has one paper behavior in scope. Promoting the pure helper to a Protocol is a mechanical refactor the day a second real implementation exists.

## Instrument metadata: additive `maker_fee` / `taker_fee` rates

`InstrumentSpec` gains **`maker_fee`** and **`taker_fee`**: signed `Decimal` rates on notional (positive = cost, negative = rebate), **defaulting to `0`** so a frictionless spec stays valid and existing paper configs are unaffected. Additive metadata per ADR-0017/0030, sourced from paper config or the venue meta. They are the **paper** computation input; the live path ignores them for accrual (it reads the venue's actual fee), so the two never disagree on a number the venue is the authority for.

**(A maker fill was finally observed, and it is *positive* — [#152](https://github.com/MarcosACH/tickwright/issues/152).** Every fill [#142](https://github.com/MarcosACH/tickwright/issues/142) captured was taker, leaving the maker side of this ADR unexercised. A post-only bid filled on testnet:

```json
{"coin":"BTC","px":"65239.0","sz":"0.002","crossed":false,"fee":"0.019571","feeToken":"USDC"}
```

`0.002 × 65239 × 0.015 % = 0.0195717` — the venue's **base maker rate, and a cost, not a rebate**. So the shorthand *"negative = maker rebate"* used throughout this ADR is right about the **sign convention** and misleading about **when it fires**: `crossed: false` does **not** imply a negative fee. A negative `fee` requires a maker-**rebate volume tier**, which is a property of the account's 14-day volume, not of the fill's liquidity side. On a fresh account every maker fill is a positive `+0.015 %` cost.

Nothing in the design changes — the field is signed, the live path reads whatever the venue reports, and `maker_fee` stays a signed rate that *may* be negative. Two consequences worth stating: an operator modelling Hyperliquid should **configure** paper's `maker_fee` to the **positive** base `0.015 %` rather than to a rebate, unless they are deliberately modelling a rebate volume tier — the *field* default stays `0` per the paragraph above, which is a frictionless-spec guarantee, not a claim about the venue; and the negative branch remains **unobserved**, so anyone hunting it on a low-volume account will not find it.**)**

## Maker vs taker is decided at the fill boundary and consumed there

The paper exchange already knows liquidity provision structurally, so the rule reads off the existing matching semantics:

- **Taker** iff the fill occurs on the order's **arrival** — a MARKET (always), or a LIMIT marketable-on-arrival that crosses the spread.
- **Maker** iff the fill comes off the **resting book** on a later crossing tick. `post_only` is the maker-only guarantee (already rejected if it would cross).

This maps 1:1 onto the venue's `crossed` flag (R2: `crossed == true ⇒ taker`). The maker/taker bit is a **fill-boundary intermediate** — it selects the paper rate (or equals `crossed` on live), is baked into the signed `fee`, and is **not** stored on the fill event: no v1 consumer reads it, and the map's destination (reported accounting) has no maker/taker analytics. It is a cheap additive field the day an execution-quality consumer needs it.

## The fill-event fee field: one signed `Decimal`, USDC implicit

The fill-family shape grows a single **`fee: Decimal`** — the raw `FillReport` (where each `Exchange` is the authority for the value) and the canonical `OrderFillEvent` (`OrderPartiallyFilled` / `OrderFilled`), which the `ExecutionManager` stamps by propagating the report's fee. **One fee per fill** (per `trade_id`); a redelivered or reconciler-synthesized copy of a `trade_id` collapses under the existing dedup key (ADR-0025), so a fee is never double-accrued.

Currency is **implicit USDC**, consistent with ADR-0029's bare-`Decimal` money convention (money is never a value+currency pair anywhere in v1) and the map's USDC-settled scope. The live `HyperliquidExchange` **validates `feeToken == "USDC"` at ingress and fails fast** otherwise, guarding the assumption rather than carrying a token constant. Spot fees may one day charge a non-USDC token (ADR-0030, deferred); the frozen `kw_only` events make adding a `fee_currency` field a one-line additive change **when spot actually lands**, not a speculative field now.

## Accrual: a distinct ledger line, never smeared into price or matching

The fee accrues **only** in the accounting surface (ADR-0035): the `Exchange` computes/reads it, the `ExecutionManager` applies the fill to the `PortfolioProjection` on the synchronous fill-apply path, and the fee lands as its **own cumulative Tier-1 ledger line** — distinct from realized PnL and from average entry price (R2: the venue keeps fee separate from `closedPnl` and from entry). It reduces the account's cash/equity (a rebate credits). On live it reconciles against venue truth, **exact at venue precision, heal + alert** (ADR-0034). The paper `FillModel` still emits **price + quantity only** — the fee is computed *after* matching, on the fill report — so money math never enters the matching path, the principle ADR-0013 was protecting.

## Consequences

- **Supersedes ADR-0013 in part** (fees): the fee deferral is lifted; the **margin** deferral still stands (rung-3 scope ceiling — a future map); ADR-0013's fill-boundary / no-smear principle is **affirmed** and carried forward. ADR-0013 gets a header note (docs-sync).
- **Sign convention (R2):** `fee > 0` = cost debited; `fee < 0` = maker rebate credited; settled in USDC.
- **Downstream:** the implementation is a later `/to-spec → /to-tickets → /tdd` slice, not this planning ticket. The `InstrumentSpec` fee fields, the `FillReport`/`OrderFillEvent` `fee` field, the pure `domain` fee helper, the paper maker/taker wiring, and the live `feeToken` ingress guard land together as one vertical slice; the `PortfolioProjection` fee accrual builds on ADR-0035's surface.
- **Funding is separate** ([#117](https://github.com/MarcosACH/tickwright/issues/117)) — a periodic cash adjustment, not a per-fill fee — and shares only the "own ledger line, not entry price" treatment.
