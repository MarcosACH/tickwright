# Hyperliquid perps: leverage, margin, liquidation price, mark/oracle price

**Purpose:** the exact math and exact field/endpoint names a reported-margin trade-economics model needs for Hyperliquid perpetuals (USDC-settled, one netted position per symbol).

**Captured:** 2026-07-22, against the Hyperliquid GitBook docs (live) and `hyperliquid-python-sdk` at tag **`0.24.0`** (the version this repo pins). SDK GitHub tag is `0.24.0`, no `v` prefix.

**Wayfinder context:** resolves research ticket **#110** (child of map **#107** "Trade economics — accounting/portfolio surface (perps)"). This is a **reference document, not a decision** — the modelling decision happens later on the wayfinder ticket / an ADR.

Notation: `mark_price` = mark price, `szi` = signed position size in coins (Hyperliquid's field name), `|szi|` = its magnitude. Formulas quoted inside `` `code` `` and labelled "verbatim" are copied word-for-word from the cited source; everything labelled **inferred / needs confirmation** is my interpretation and is not a source claim.

---

## 1. Leverage

### 1.1 Per-asset maximum leverage — where it comes from, how it's queried

Max leverage is a per-asset (per-tier) protocol setting. It is returned in the perp metadata:

- `meta` info request → `universe[].maxLeverage` (also `marginTableId`, `onlyIsolated`). The SDK `meta()` docstring is out of date and only lists `name`/`szDecimals`, but the live response and the `metaAndAssetCtxs` docstring both carry `maxLeverage`. (SDK `info.py:273-289`; docstring omission noted below.)
- `metaAndAssetCtxs` info request → element `[0].universe[]` has `name, szDecimals, maxLeverage, onlyIsolated`. (SDK `info.py:291-324`, docstring verbatim.)

Mainnet max leverage is asset- and tier-dependent (verbatim from margin-tiers page):

| Asset / group | Max leverage (tier 0 notional band) | Higher tier |
|---|---|---|
| BTC | 40x (0–150M USDC) | 20x (>150M) |
| ETH | 25x (0–100M USDC) | 15x (>100M) |
| SOL | 20x (0–70M USDC) | 10x (>70M) |
| XRP | 20x (0–40M USDC) | 10x (>40M) |
| Group 1 (AAVE, ADA, AVAX, DOGE, HYPE, LINK, LTC, SUI, UNI, …) | 10x (0–20M) | 5x (>20M) |
| Group 2 (ARB, BNB, DOT, TON, TRX, …) | 10x (0–3M) | 5x (>3M) |

The doc says the whole range is **3–40x** across assets. Source: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/margin-tiers

### 1.2 How leverage is set

Verbatim (margining page): leverage "can be set by a user to any integer between 1 and the max leverage." Source: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/margining

Two on-chain actions:

- **Set per-asset leverage, cross or isolated** — action `updateLeverage` with `{asset, isCross: bool, leverage: int}`. SDK: `update_leverage(self, leverage: int, name: str, is_cross: bool = True)`. (`hyperliquid-dex/hyperliquid-python-sdk@0.24.0 hyperliquid/exchange.py:389-409`.) `is_cross=True` selects cross margin for that asset; `is_cross=False` selects isolated at that leverage.
- **Add/remove isolated margin on an open position** — action `updateIsolatedMargin` with `{asset, isBuy: True, ntli: <usd int>}`. SDK: `update_isolated_margin(self, amount: float, name: str)`. (`…@0.24.0 hyperliquid/exchange.py:411-432`.) This is how isolated collateral is topped up / withdrawn; it does not exist for cross.

The current per-asset leverage a user has selected is reported back as `leverage.value` (int) with `leverage.type` ∈ {`cross`,`isolated`} in `clearinghouseState`. SDK type: `CrossLeverage {type:"cross", value:int}`, `IsolatedLeverage {type:"isolated", value:int, rawUsd:str}`. (`…@0.24.0 hyperliquid/utils/types.py:82-97`.)

### 1.3 "Effective leverage"

**Inferred / needs confirmation.** Hyperliquid's docs and API do **not** define a term "effective leverage." The value returned by the API (`leverage.value`) is the *user-set cap*, not the realized ratio. The standard convention (not sourced from HL) for an open position is:

```
effective_leverage = position_notional / account_value = positionValue / accountValue
```

where `positionValue` is the per-position field and `accountValue` is `marginSummary.accountValue` (cross) — both from `clearinghouseState` (§2.3). Position notional itself is `|szi| * mark_price` (from the initial-margin formula, §2.1). Treat the exact denominator (whole-account `accountValue` vs. per-position isolated equity) as a modelling choice to confirm on the ADR.

---

## 2. Margin

### 2.1 Initial-margin fraction

Verbatim (margining page): initial margin required to open =

```
position_size * mark_price / leverage
```

Source: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/margining

Therefore the **initial-margin fraction** is `1 / leverage`, and at the asset's cap it is `1 / max_leverage`. (Inferred algebra from the verbatim formula; not separately stated.)

### 2.2 Maintenance-margin fraction and its relation to max leverage

Verbatim (margining / liquidations): maintenance margin is "half of the initial margin at max leverage." So:

```
maintenance_margin_fraction = 1 / (2 * max_leverage)     # base tier
```

Verbatim ranges (liquidations page): "between 1.25% (for 40x max leverage assets) and 16.7% (for 3x max leverage assets)." Check: 1/(2·40)=1.25%, 1/(2·3)=16.7%. Sources: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/liquidations , https://hyperliquid.gitbook.io/hyperliquid-docs/trading/margining

**With margin tiers** (large positions), maintenance margin is piecewise-linear. Verbatim (margin-tiers page):

```
maintenance_margin = notional_position_value * maintenance_margin_rate - maintenance_deduction
maintenance_margin_rate(tier = n) = (Initial Margin Rate at Maximum leverage at tier n) / 2
maintenance_deduction(tier = 0) = 0
```

`maintenance_deduction` rises at higher tiers to keep the maintenance-margin function continuous across tier boundaries. Example given: at 20x, `maintenance_margin_rate` = 2.5%. Source: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/margin-tiers

### 2.3 Cross vs isolated — what each reserves, and account-value / margin-used / available-margin

Verbatim margin-mode definitions (margining page):

- **Cross margin** (default): "allows for maximal capital efficiency by sharing collateral between all other cross margin positions." Unrealized PnL "automatically [becomes] available as initial margin for new positions."
- **Isolated margin**: "allows an asset's collateral to be constrained to that asset." Unrealized PnL is applied "as additional margin for the open position" only.
- **Strict isolated**: isolated, but margin cannot be removed.
- **No cross (HIP-3)**: isolated with margin removal enabled, but no cross.

Source: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/margining

These quantities come out of the `clearinghouseState` info response (SDK `info.py:86-128`, docstring verbatim):

- `marginSummary` and `crossMarginSummary`, each a `MarginSummary { accountValue, totalMarginUsed, totalNtlPos, totalRawUsd }` (all float-strings).
  - `accountValue` = account equity (collateral marked to mark price).
  - `totalNtlPos` = total notional of open positions = Σ `positionValue`.
  - `totalMarginUsed` = margin currently locked by open positions.
  - `totalRawUsd` = raw USDC balance component.
- Per position, `assetPositions[].position`: `marginUsed`, `positionValue`, `unrealizedPnl`, `entryPx`, `szi`, `liquidationPx`, `returnOnEquity`, `leverage`. For isolated, `leverage.rawUsd` holds that position's isolated USDC.

**Inferred / needs confirmation** (field algebra the docs do not spell out explicitly):
- `accountValue ≈ totalRawUsd + Σ unrealizedPnl` (equity = balance + mark-to-mark PnL).
- available (free) margin `≈ accountValue - totalMarginUsed`.
- For an **isolated** position, the position's equity is its `leverage.rawUsd + unrealizedPnl`, i.e. `marginUsed` plus/minus PnL, decoupled from cross equity.

**Transfer / withdrawal constraint** (verbatim, margining page): withdrawing unrealized profit requires

```
transfer_margin_required = max(initial_margin_required, 0.1 * total_position_value)
```

i.e. "remaining margin is at least 10% of the total notional position value."

**Liquidation condition, cross** (verbatim, margining page): the account is liquidated when

```
account_value < maintenance_margin * total_open_notional_position
```

**Liquidation condition, isolated**: same rule applied to the isolated margin and that position's notional only.

---

## 3. Liquidation price

Verbatim canonical formula (liquidations page):

```
liq_price = price - side * margin_available / position_size / (1 - l * side)
```

where (verbatim definitions):

- `l = 1 / MAINTENANCE_LEVERAGE`. "For assets with margin tiers, maintenance leverage depends on the unique margin tier corresponding to the position value at the liquidation price." (So `l` = the maintenance-margin fraction of §2.2, tier-resolved.)
- `side = 1 for long and -1 for short`.
- `margin_available (cross) = account_value - maintenance_margin_required`.
- `margin_available (isolated) = isolated_margin - maintenance_margin_required`.

Source: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/liquidations

**Not defined by the docs (inferred / needs confirmation):**
- `price` — the reference price at which `margin_available` is currently measured. Inferred: the current **mark price** used to value the position. Needs confirmation.
- `position_size` — inferred: the position magnitude in coins, `|szi|`. The formula is dimensionally a price = price − (USD) / (coins) / (dimensionless), consistent with `position_size` in coins. Needs confirmation.
- `maintenance_margin_required` — from §2.2: `|szi| * mark_price * maintenance_margin_fraction` at base tier, or the piecewise `notional * mmr − deduction` with tiers.

**API field:** the protocol also returns the computed liquidation price directly as `liquidationPx` (optional float-string) in `clearinghouseState → assetPositions[].position.liquidationPx`. (SDK `info.py:106`.) A model can consume `liquidationPx` directly rather than re-deriving it.

**Liquidation trigger** (verbatim, liquidations page): "A liquidation event occurs when a trader's positions move against them to the point where the account equity falls below the maintenance margin." Backstop (liquidator-vault) takeover triggers when equity drops below **two-thirds of the maintenance margin**. No clearance fee on liquidations.

---

## 4. Mark price / oracle price

### 4.1 Oracle price

Verbatim (robust-price-indices page): "This weighted median of CEX prices is robust because it does not depend on hyperliquid's market data at all." It is used to compute funding rates and updates roughly every **3 seconds**. Source: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/robust-price-indices

The doc does not enumerate the per-CEX weights *for the oracle* (only for the mark's CEX component, below). Weight list for the oracle: **needs confirmation**.

### 4.2 Mark price

Verbatim (robust-price-indices page): "Mark price is the median of the following prices:

1. Oracle price plus a 150 second exponential moving average (EMA) of the difference between Hyperliquid's mid price and the oracle price
2. The median of best bid, best ask, last trade on Hyperliquid
3. Median of Binance, OKX, Bybit, Gate IO, MEXC perp mid prices with weights 3, 2, 2, 1, 1, respectively"

Verbatim conditional clause: "If exactly two out of the three inputs above exist, the 30 second EMA of the median of best bid, best ask, and last trade on Hyperliquid is also added to the median inputs." (Ensures the median is well-defined.)

Verbatim EMA recurrence:

```
ema = numerator / denominator
numerator   -> numerator   * exp(-t / 2.5 minutes) + sample * t
denominator -> denominator * exp(-t / 2.5 minutes) + t
```

Source: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/robust-price-indices

### 4.3 Mark vs last-trade vs oracle — and why it matters

- **Mark price** is a robust median (§4.2), updated ~every 3s. It is what "is used for margining, liquidations, and PnL calculations."
- **Last-trade price** is only *one* of several inputs into component 2 of the mark; it is not itself the mark.
- **Oracle price** (§4.1) is CEX-only, ignores the HL book, and is used for funding — it is component-1's anchor but not the mark.
- **Load-bearing for the model:** unrealized PnL, margin usage, and liquidation price all key off **mark price**, not last-trade. (Robust-price-indices page: mark is "used for margining, liquidations, and PnL calculations.")

### 4.4 How mark and oracle are published / queried

- **`metaAndAssetCtxs`** info request → per-asset ctx carries `markPx`, `oraclePx`, `midPx`, `premium`, `funding`, `openInterest`, `impactPxs`, `prevDayPx`, `dayNtlVlm`. SDK docstring `info.py:291-324`; SDK type `PerpAssetCtx` (`…@0.24.0 hyperliquid/utils/types.py:99-113`).
- **`activeAssetCtx`** websocket channel → `data.ctx` is a `PerpAssetCtx` with the same `markPx`/`oraclePx`/`midPx` fields, per coin. SDK type `ActiveAssetCtx {coin, ctx: PerpAssetCtx}` and `ActiveAssetCtxMsg` (`…@0.24.0 hyperliquid/utils/types.py:114-116`); subscription `ActiveAssetCtxSubscription {type:"activeAssetCtx", coin}` (`types.py:52`).
- **`activeAssetData`** websocket channel → per-(user,coin): `leverage`, `maxTradeSzs`, `availableToTrade`, `markPx`. (`…@0.24.0 hyperliquid/utils/types.py:121-131`.)
- **`allMids`** info request / websocket channel → `mids: Record<coin, string>`. These are **mid prices, not mark prices** — do not use for margining. SDK `all_mids()` `info.py:187-199`; subscription `AllMidsSubscription {type:"allMids"}` (`types.py:39`).
- **`webData2`** websocket channel → aggregate user/web state (includes clearinghouse-style data). SDK subscription `WebData2Subscription {type:"webData2", user}` (`…@0.24.0 hyperliquid/utils/types.py:51`). **Discrepancy:** the current live GitBook websocket page lists this as `webData3`; the pinned SDK (0.24.0) still defines `webData2`. Use `webData2` with this SDK; confirm the channel name against the endpoint before relying on it.

---

## Fields & endpoints quick-reference

| Field | What it is | Info endpoint (`type`) / WS channel | SDK accessor @0.24.0 |
|---|---|---|---|
| `maxLeverage` | Per-asset max leverage cap | `meta`, `metaAndAssetCtxs` (`universe[]`) | `Info.meta()` `info.py:273`; `Info.meta_and_asset_ctxs()` `info.py:291` |
| `marginTableId`, `onlyIsolated` | Margin-tier table id / isolated-only flag | `meta`, `metaAndAssetCtxs` (`universe[]`) | same as above |
| `markPx` | Mark price (margining/PnL/liq) | `metaAndAssetCtxs`; WS `activeAssetCtx`, `activeAssetData` | `Info.meta_and_asset_ctxs()` `info.py:291`; WS `PerpAssetCtx` |
| `oraclePx` | Oracle price (funding) | `metaAndAssetCtxs`; WS `activeAssetCtx` | `Info.meta_and_asset_ctxs()` `info.py:291` |
| `midPx` | Mid price | `metaAndAssetCtxs`; WS `activeAssetCtx` | `Info.meta_and_asset_ctxs()` `info.py:291` |
| `funding`, `premium`, `openInterest` | Funding / premium / OI | `metaAndAssetCtxs`; WS `activeAssetCtx` | `Info.meta_and_asset_ctxs()` `info.py:291` |
| mids (`Record<coin,str>`) | Mid prices (NOT mark) | `allMids`; WS `allMids` | `Info.all_mids()` `info.py:187` |
| `accountValue` | Account equity | `clearinghouseState` (`marginSummary`, `crossMarginSummary`) | `Info.user_state(addr)` `info.py:86` |
| `totalNtlPos` | Σ position notional | `clearinghouseState` (`*MarginSummary`) | `Info.user_state(addr)` `info.py:86` |
| `totalMarginUsed` | Margin locked | `clearinghouseState` (`*MarginSummary`) | `Info.user_state(addr)` `info.py:86` |
| `totalRawUsd` | Raw USDC balance | `clearinghouseState` (`*MarginSummary`) | `Info.user_state(addr)` `info.py:86` |
| `withdrawable` | Withdrawable balance | `clearinghouseState` | `Info.user_state(addr)` `info.py:86` |
| `positionValue` | Position notional (per pos) | `clearinghouseState` (`assetPositions[].position`) | `Info.user_state(addr)` `info.py:86` |
| `marginUsed` | Margin used (per pos) | `clearinghouseState` (`assetPositions[].position`) | `Info.user_state(addr)` `info.py:86` |
| `unrealizedPnl` | Unrealized PnL @ mark (per pos) | `clearinghouseState` (`assetPositions[].position`) | `Info.user_state(addr)` `info.py:86` |
| `entryPx` | Entry price (per pos) | `clearinghouseState` (`assetPositions[].position`) | `Info.user_state(addr)` `info.py:86` |
| `szi` | Signed position size, coins (per pos) | `clearinghouseState` (`assetPositions[].position`) | `Info.user_state(addr)` `info.py:86` |
| `liquidationPx` | Liquidation price (per pos) | `clearinghouseState` (`assetPositions[].position`) | `Info.user_state(addr)` `info.py:86` |
| `leverage.{type,value,rawUsd}` | User-set leverage / isolated USDC | `clearinghouseState` (`assetPositions[].position`) | `Info.user_state(addr)` `info.py:86` |
| set leverage | Action `updateLeverage {asset,isCross,leverage}` | (exchange endpoint) | `Exchange.update_leverage(lev,name,is_cross)` `exchange.py:389` |
| add/remove isolated margin | Action `updateIsolatedMargin {asset,isBuy,ntli}` | (exchange endpoint) | `Exchange.update_isolated_margin(amount,name)` `exchange.py:411` |

---

## Open questions / needs confirmation

1. **`price` in the liquidation formula** — docs do not define it. Inferred to be the current mark price. Confirm.
2. **`position_size` in the liquidation formula** — docs do not state units. Inferred `|szi|` in coins (dimensional analysis supports it). Confirm.
3. **Effective leverage** — not an HL-documented term. The `notional/equity` relation is a convention; the correct denominator for isolated positions (whole-account equity vs. per-position isolated equity) is a modelling choice. Confirm on ADR.
4. **Field algebra `accountValue = totalRawUsd + Σ unrealizedPnl` and `available = accountValue − totalMarginUsed`** — inferred, not stated verbatim. Validate against a real `clearinghouseState` payload.
5. **Oracle-price CEX weights** — the robust-price-indices page gives per-CEX weights for the *mark's* CEX component (Binance/OKX/Bybit/Gate/MEXC = 3/2/2/1/1) but not for the *oracle* median. Confirm the oracle's exchange set + weights.
6. **`webData2` vs `webData3`** — SDK 0.24.0 defines `webData2`; the live docs page lists `webData3`. Confirm the channel name in force for the endpoint you connect to.
7. **`meta()` SDK docstring is stale** — it lists only `name`/`szDecimals`, but live responses include `maxLeverage`/`marginTableId`. Rely on the live payload / `metaAndAssetCtxs` docstring, not the `meta()` docstring.
8. **Tier resolution for `l`** — `MAINTENANCE_LEVERAGE` is tier-dependent and the tier is "the unique margin tier corresponding to the position value at the liquidation price," i.e. self-referential. Re-deriving `liquidationPx` from scratch requires solving that fixed point; consuming the API's `liquidationPx` avoids it.

---

## Sources

Docs (fetched 2026-07-22):
- Margining — https://hyperliquid.gitbook.io/hyperliquid-docs/trading/margining
- Margin tiers — https://hyperliquid.gitbook.io/hyperliquid-docs/trading/margin-tiers
- Liquidations — https://hyperliquid.gitbook.io/hyperliquid-docs/trading/liquidations
- Robust price indices (mark/oracle) — https://hyperliquid.gitbook.io/hyperliquid-docs/trading/robust-price-indices
- Perpetuals info endpoint — https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals
- Websocket subscriptions — https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions

SDK — `hyperliquid-dex/hyperliquid-python-sdk@0.24.0` (read via GitHub contents API at tag `0.24.0`):
- `hyperliquid/info.py:86-128` — `Info.user_state` → `clearinghouseState` (docstring lists all `assetPositions`/`marginSummary` fields)
- `hyperliquid/info.py:187-199` — `Info.all_mids` → `allMids`
- `hyperliquid/info.py:273-289` — `Info.meta` → `meta` (stale docstring)
- `hyperliquid/info.py:291-324` — `Info.meta_and_asset_ctxs` → `metaAndAssetCtxs` (docstring lists `markPx`/`oraclePx`/`midPx`/`maxLeverage`/`onlyIsolated`)
- `hyperliquid/exchange.py:389-409` — `Exchange.update_leverage` → action `updateLeverage {asset,isCross,leverage}`
- `hyperliquid/exchange.py:411-432` — `Exchange.update_isolated_margin` → action `updateIsolatedMargin {asset,isBuy,ntli}`
- `hyperliquid/utils/types.py:39-70` — subscription TypedDicts (`allMids`, `activeAssetCtx`, `activeAssetData`, `webData2`, …)
- `hyperliquid/utils/types.py:82-97` — `CrossLeverage` / `IsolatedLeverage` (`type,value[,rawUsd]`)
- `hyperliquid/utils/types.py:99-131` — `PerpAssetCtx` (`markPx,oraclePx,midPx,…`), `ActiveAssetCtx`, `ActiveAssetData`
