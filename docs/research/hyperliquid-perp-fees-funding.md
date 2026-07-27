# Hyperliquid perpetual fees & funding mechanics

**Research date:** 2026-07-22
**Pinned SDK:** `hyperliquid-python-sdk==0.24.0` (repo `pyproject.toml` `~=0.24`; `uv.lock` resolves
to `0.24.0`). SDK source cited at git tag `0.24.0` (the GitHub release is titled *v0.24.0*; the git
tag itself has no `v` prefix).
**Wayfinder ticket:** research #109 (child of map #107 — *Trade economics — accounting/portfolio
surface (perps)*).

Scope: factual capture of the venue's fee and funding mechanics against primary sources (official
docs + pinned SDK source). This is a reference document, **not** a design proposal — it makes no
recommendation about how Tickwright should model these facts.

Citation convention:

- Docs: a URL to the owning page. All docs URLs are under
  `https://hyperliquid.gitbook.io/hyperliquid-docs`.
- SDK: `hyperliquid-python-sdk@0.24.0 <path>:L<n>` — line numbers are against the file at git tag
  `0.24.0`.
- A claim with no citation is a lead, not a finding, and is marked **(unverified)**.

> **Status: point-in-time capture, superseded in part — the ADRs are canonical.**
> Kept **verbatim** as the evidence behind ADR-0034 – ADR-0046 and **not** maintained; where it
> disagrees with an ADR, the ADR wins (`CLAUDE.md` docs-sync policy). Measured against a live venue
> by [#142](https://github.com/MarcosACH/tickwright/issues/142) and
> [#152](https://github.com/MarcosACH/tickwright/issues/152):
>
> | in this note | current answer |
> | --- | --- |
> | *"`fee` negative = maker rebate"* | **The sign convention holds; the shorthand misleads.** `crossed: false` does **not** imply a negative fee — a measured maker fill charged the base `+0.015 %` as a *cost*. The rebate branch is gated on a maker-rebate **volume tier**, not on maker-ness, and remains unobserved — [ADR-0036](../adr/0036-perp-fee-model.md). |
> | `userFunding.usdc` sign — *"prose-unverified"* | **Adopted and specified:** `amount = − signed_size × price × funding_rate`, negative = paid, ingested verbatim on live — [ADR-0037](../adr/0037-perp-funding-model.md). |
> | `feeToken == "USDC"` literal — *"inferred, not quoted"* | **Observed** on live testnet fills (#142/#152); the live ingress fail-fasts on any other value — [ADR-0036](../adr/0036-perp-fee-model.md). |
>
> Also settled downstream: `closedPnl` is **gross of fees** — 65/65 opening fills read `0.0` against a
> non-zero `fee`, re-confirmed on a maker fill (#142/#152), so the adapter needs no
> gross-normalization ([ADR-0045](../adr/0045-economic-terminology-and-the-closed-event-catalog.md)).

---

## Fees

### 1. Base rates and the 14-day volume-tier schedule (perps)

Fees are set by a **14-day rolling weighted volume** tier, with one fee tier applied across all
assets (perps, HIP-3 perps, and spot). Tier 0 (base) is **0.045% taker / 0.015% maker**; the
schedule falls to **0.024% taker / 0.000% maker** at tier 6 (>$7B).
[docs/trading/fees]

| Tier | 14d weighted volume | Taker (perps) | Maker (perps) |
| ---- | ------------------- | ------------- | ------------- |
| 0    | (base)              | 0.045%        | 0.015%        |
| 1    | > $5M               | 0.040%        | 0.012%        |
| 2    | > $25M              | 0.035%        | 0.008%        |
| 3    | > $100M             | 0.030%        | 0.004%        |
| 4    | > $500M             | 0.028%        | 0.000%        |
| 5    | > $2B               | 0.026%        | 0.000%        |
| 6    | > $7B               | 0.024%        | 0.000%        |

[docs/trading/fees]

The **live** per-address tier is exposed by the `userFees` info request as `userCrossRate` (taker)
and `userAddRate` (maker), with the full `feeSchedule` (`add`=maker, `cross`=taker) and its
`tiers.vip` / `tiers.mm` breakdown.
[`hyperliquid-python-sdk@0.24.0 hyperliquid/info.py:L506-L547`]

### 2. HYPE staking discounts

A separate **staking discount** multiplies the tier rate. The discounted rate =
`base_rate × (1 − discount)`. (Verified against the docs' expanded fee table: tier-0 taker
0.045% × (1 − 0.40 Diamond) = 0.0270%, × (1 − 0.30 Platinum) = 0.0315%, × (1 − 0.20 Gold) =
0.0360%, etc. — all match the published cells.)
[docs/trading/fees]

| Staking tier | HYPE staked | Trading-fee discount |
| ------------ | ----------- | -------------------- |
| Wood         | > 10        | 5%                   |
| Bronze       | > 100       | 10%                  |
| Silver       | > 1,000     | 15%                  |
| Gold         | > 10,000    | 20%                  |
| Platinum     | > 100,000   | 30%                  |
| Diamond      | > 500,000   | 40%                  |

[docs/trading/fees]

### 3. Maker rebates

High **maker-volume-share** addresses earn a negative maker fee (rebate). The rebate is stated as a
negative percentage and "paid out continuously on each trade directly to the trading wallet"
[docs/trading/fees]:

| Rebate tier | 14d weighted maker-volume share | Maker fee |
| ----------- | ------------------------------- | --------- |
| 1           | > 0.5%                          | −0.001%   |
| 2           | > 1.5%                          | −0.002%   |
| 3           | > 3.0%                          | −0.003%   |

[docs/trading/fees]

Because a maker fee can be negative, an individual fill's `fee` can be **negative = a rebate
credited** rather than a cost. The WebSocket schema states this explicitly:
`fee: string // negative means rebate`. [docs/for-developers/api/websocket/subscriptions]

### 4. Referral discount (peripheral)

The docs note "Referral rewards apply for a user's first $1B in volume and referral discounts apply
for a user's first $25M in volume." [docs/trading/fees] The exact referral-discount percentage was
not captured verbatim; the live value is exposed as `activeReferralDiscount` (and
`feeSchedule.referralDiscount`) in the `userFees` response
[`hyperliquid-python-sdk@0.24.0 hyperliquid/info.py:L512-L545`]. Treat the exact % as
**(unverified)**.

### 5. Maker vs taker determination, and per-fill reporting

- **Determination:** a fill is a **taker** when the incoming order crossed the spread; a **maker**
  when it rested and was crossed into. The venue reports this per fill via the boolean `crossed`,
  documented as `crossed: boolean // whether order crossed the spread (was taker)`.
  So `crossed == true` ⇒ taker; `crossed == false` ⇒ maker.
  [docs/for-developers/api/websocket/subscriptions]
- **Per-fill fee is reported:** every fill carries its own `fee` (string) and `feeToken` (string),
  so the fee **and** the maker/taker distinction are available per fill — we do not need to
  reconstruct them from the tier schedule.
  [`hyperliquid-python-sdk@0.24.0 hyperliquid/utils/types.py:L132-L150`]

### 6. Settlement currency and sign of the fee

- **Currency:** perps are collateralised and settled in **USDC** ("you use USDC as collateral to
  long or short the token") [docs/trading/margining]. For a perp fill the `feeToken` is therefore
  `"USDC"`; the field exists to disambiguate (spot fills may be charged in the received token). The
  `"USDC"` literal for perp fills is inferred from the USDC-collateral fact rather than quoted from a
  fill example — treat the exact literal as **(unverified)**, but the currency (USDC) is verified.
- **Sign:** `fee` is a positive number for a cost **debited**, and **negative for a rebate credited**
  (`fee: string // negative means rebate`).
  [docs/for-developers/api/websocket/subscriptions]

### Fill payload — field table

Source of record for the pinned version is the `Fill` TypedDict. The REST `userFills`
[`info.py:L201-L228`] and WS `userFills` / `userEvents` channels all deliver this same shape.
[`hyperliquid-python-sdk@0.24.0 hyperliquid/utils/types.py:L132-L155`]

| Field           | Type   | Meaning                                                        | Citation |
| --------------- | ------ | ------------------------------------------------------------- | -------- |
| `coin`          | str    | Asset name (e.g. `"ETH"`)                                     | types.py:L134 |
| `px`            | str    | Fill price (numeric string → `Decimal`)                       | types.py:L135 |
| `sz`            | str    | Fill size (numeric string → `Decimal`)                        | types.py:L136 |
| `side`          | `"A"`/`"B"` | Side; `"A"`=ask/sell, `"B"`=bid/buy                      | types.py:L137, L15 |
| `time`          | int    | Fill timestamp, Unix ms                                       | types.py:L138 |
| `startPosition` | str    | Signed position size in the coin **before** this fill         | types.py:L139 |
| `dir`           | str    | Human display direction (e.g. `"Open Long"`, `"Close Short"`) | types.py:L140 |
| `closedPnl`     | str    | Realized PnL from the closing portion of this fill (USDC)     | types.py:L141 |
| `hash`          | str    | L1 transaction hash (`0x0…0` for internal/funding-style)      | types.py:L142 |
| `oid`           | int    | Order id                                                      | types.py:L143 |
| `crossed`       | bool   | `true`⇒taker (crossed the spread); `false`⇒maker             | types.py:L145 |
| `fee`           | str    | Fee in `feeToken`; **negative = rebate** credited            | types.py:L146 |
| `tid`           | int    | Unique trade id                                              | types.py:L147 |
| `feeToken`      | str    | Token the fee is charged in (USDC for perps)                 | types.py:L148 |

`crossed`, `fee`, `feeToken` semantics per docs/for-developers/api/websocket/subscriptions.

**`builderFee` caveat:** the WebSocket docs list an optional `builderFee?: string` ("amount paid to
builder") on a fill, but the pinned SDK's `Fill` TypedDict at `0.24.0` does **not** include a
`builderFee` field [`types.py:L132-L150`]. If we need builder-fee data we cannot rely on the SDK's
static type — read it off the raw payload. (`BuilderInfo` at `types.py:L185` is the *order-placement*
builder-fee input `{b, f}`, unrelated to the per-fill report.)

---

## Funding

### 1. Source and how it is published / queried

Funding is a per-asset, venue-computed rate published in the perp asset context and queryable both
per-asset (history) and per-account (payments). Relevant info-endpoint request `type` strings and WS
channels:

| Purpose                              | `type` / channel        | SDK wrapper (0.24.0)                 | Citation |
| ------------------------------------ | ----------------------- | ----------------------------------- | -------- |
| Current funding per asset (snapshot) | `metaAndAssetCtxs`      | `Info.meta_and_asset_ctxs()`        | info.py:L291-L324 |
| Current funding per asset (WS)       | `activeAssetCtx`        | subscribe `ActiveAssetCtxSubscription` | types.py:L52, L114-L116 |
| Predicted next funding (multi-venue) | `predictedFundings`     | **none — call `post("/info", {"type":"predictedFundings"})`** | docs (below) |
| Per-asset funding history            | `fundingHistory`        | `Info.funding_history(name, startTime, endTime)` | info.py:L402-L428 |
| Per-account funding payments (REST)  | `userFunding`           | `Info.user_funding_history(user, startTime, endTime)` | info.py:L430-L446 |
| Per-account funding payments (WS)    | `userFundings`          | subscribe `UserFundingsSubscription` | types.py:L47, L66 |

- The per-asset **current funding rate** is the `funding` field of `PerpAssetCtx`
  (a numeric string). [`hyperliquid-python-sdk@0.24.0 hyperliquid/utils/types.py:L99-L113`;
  `info.py:L291-L324`]
- `predictedFundings` is **not** wrapped by the SDK at `0.24.0` (no method in `info.py`; `grep` for
  `predicted` is empty). Request `{"type":"predictedFundings"}` returns, per asset, a list of
  `[venue, {"fundingRate": str, "nextFundingTime": ms}]` pairs — e.g. `"HlPerp"` (Hyperliquid) and
  `"BinPerp"` (Binance) for comparison. [docs/for-developers/api/info-endpoint/perpetuals]
- `activeAssetCtx` (WS `WsActiveAssetCtx`) carries `{coin, ctx}` where `ctx` is the perp asset
  context (funding, markPx, oraclePx, …). [types.py:L114-L116; docs/…/websocket/subscriptions]

### 2. Accrual / settlement cadence and timestamp semantics

- **Cadence:** "Funding is paid **every hour**" — settled hourly, at **one eighth** of the computed
  (8-hour) rate each hour. [docs/trading/funding]
- **Premium sampling:** the premium is **sampled every 5 seconds and averaged over the hour**;
  funding for the hour is computed from that average. [docs/trading/funding]
- **Timestamp semantics:** a `fundingHistory` record's `time` (Unix ms) is the funding hour it
  describes, and its `fundingRate` is the (hourly) rate applied. A `userFunding` record's `time`
  (Unix ms) is when the payment for that hour landed on the account.
  [docs/for-developers/api/info-endpoint/perpetuals]

### 3. Exact funding formula

Verbatim from the docs [docs/trading/funding]:

```
Funding Rate (F) = Average Premium Index (P)
                 + clamp( interest_rate − Premium Index (P), −0.0005, 0.0005 )
```

- **Interest-rate component:** `0.01% every 8 hours`, i.e. `0.00125% every hour`
  (≈ 11.6% APR, paid to short). [docs/trading/funding]
- **Premium (standard perps):** `premium = impact_price_difference / oracle_price`, where
  `impact_price_difference = max(impact_bid_px − oracle_px, 0) − max(oracle_px − impact_ask_px, 0)`.
  [docs/trading/funding]
- **Premium (HIP-3 perps):** `premium = (0.5 * (impact_bid_px + impact_ask_px) / oracle_px) − 1`.
  [docs/trading/funding]
- **Clamp on the interest term:** the `(interest_rate − premium)` term is clamped to
  `[−0.0005, +0.0005]` (±0.05%). [docs/trading/funding]
- **Overall cap:** "Funding on Hyperliquid is capped at **4%/hour**." [docs/trading/funding]
- **8h vs 1h convention:** "The funding rate formula applies to the **8 hour** funding rate. However,
  funding is paid every hour at **one eighth** of the computed rate." [docs/trading/funding]

### 4. Notional basis — ORACLE price, not mark

The per-hour funding payment is:

```
funding_payment = position_size × oracle_price × funding_rate
```

with the docs stating "the **spot oracle price** is used to convert the position size to notional
value, **not the mark price**." [docs/trading/funding, docs/trading/margining]

(Cross-checked on the docs' own `userFunding` example: `szi = 49.1477` ETH, `fundingRate =
0.0000417` (hourly), `usdc = −3.625312`. At an ETH oracle ≈ $1770 the product
`49.1477 × 1770 × 0.0000417 ≈ 3.63` matches the reported magnitude, confirming the hourly-rate ×
oracle-notional basis.) [docs/for-developers/api/info-endpoint/perpetuals]

### 5. Sign convention

- **Rate sign:** when the funding rate is **positive, longs pay shorts**; when **negative, shorts
  pay longs**. [docs/trading/funding]
- **`usdc` sign on a `userFunding` record:** the docs do not state the sign in prose, but the example
  record is self-consistent with the rate convention: a **long** (`szi = +49.1477`) at a **positive**
  rate (`fundingRate = 0.0000417`) shows `usdc = −3.625312`. Positive rate ⇒ long pays ⇒ the long's
  `usdc` is negative. So **`usdc` negative = funding paid (debited); positive = funding received
  (credited)**. This is derived from the worked example and the rate convention, not an explicit
  sign statement — treat the prose statement as **(unverified)**, though the arithmetic is
  unambiguous. [docs/for-developers/api/info-endpoint/perpetuals; docs/trading/funding]

### 6. Account treatment — separate cash adjustment, not `closedPnl`

- Funding is "**added or subtracted from the balance** of contract holders at the funding interval"
  and is "directly settled in the collateral balance (USDC …), not a separate instrument."
  [docs/trading/funding, docs/trading/margining]
- It is a **separate ledger category** from realized trade PnL: the `userFunding` records
  (`delta.type == "funding"`) are distinct from fill `closedPnl`, and the venue exposes a dedicated
  `userNonFundingLedgerUpdates` endpoint — a "non-funding" ledger — which by its very name treats
  funding as its own line item, excluded from other ledger updates.
  [`hyperliquid-python-sdk@0.24.0 hyperliquid/info.py:L652-L668`;
  docs/for-developers/api/info-endpoint/perpetuals]
- Consequently funding is **not** folded into a position's entry price and **not** part of a fill's
  `closedPnl`; it is a direct USDC cash adjustment tracked separately.

### `userFunding` payload — field table

REST `userFunding` returns records of `{delta, hash, time}` where `delta` is a `"funding"` ledger
delta. [docs/for-developers/api/info-endpoint/perpetuals;
`hyperliquid-python-sdk@0.24.0 hyperliquid/info.py:L430-L446`]

Example record [docs/for-developers/api/info-endpoint/perpetuals]:

```json
{
  "delta": { "coin": "ETH", "fundingRate": "0.0000417", "szi": "49.1477",
             "type": "funding", "usdc": "-3.625312", "nSamples": null },
  "hash": "0xa166e3fa63c25663024b03f2e0da011a00307e4017465df020210d3d432e7cb8",
  "time": 1681222254710
}
```

| Field               | Type          | Meaning                                                           | Citation |
| ------------------- | ------------- | ---------------------------------------------------------------- | -------- |
| `delta.type`        | str           | Ledger-delta type; `"funding"` for a funding payment            | info-endpoint/perpetuals |
| `delta.coin`        | str           | Perp asset the payment is for                                   | info-endpoint/perpetuals |
| `delta.fundingRate` | str→`Decimal` | Hourly funding rate applied for this interval                   | info-endpoint/perpetuals |
| `delta.szi`         | str→`Decimal` | Signed position size held over the interval (+ long, − short)   | info-endpoint/perpetuals |
| `delta.usdc`        | str→`Decimal` | USDC payment; **− = paid (debit), + = received (credit)** *(sign derived, see §5)* | info-endpoint/perpetuals |
| `delta.nSamples`    | int \| null   | Number of premium samples backing the rate (may be `null`)      | info-endpoint/perpetuals |
| `hash`              | str           | L1 tx hash for the funding event                               | info-endpoint/perpetuals |
| `time`              | int           | Unix ms timestamp the payment landed                           | info-endpoint/perpetuals |

**SDK typing caveat:** the pinned SDK does **not** provide a TypedDict for a `userFunding` record.
`user_funding_history` is typed `-> Any` [`info.py:L430`], the `userFundings` WS message falls under
`OtherWsMsg` with `data: Any` [`types.py:L157-L170`], and the `user_funding_history` docstring only
lists `user/type/startTime/endTime` — it does **not** document the `delta` sub-object
[`info.py:L437-L442`]. The field table above is therefore sourced from the **docs**, not the SDK
types.

**WS `userFundings` shape** (docs `WsUserFunding`) is flatter than the REST record — the docs show
`{time, coin, usdc, szi, fundingRate}` with **no `nSamples` and no `delta` wrapper**. Field names and
sign semantics match the REST `delta`. [docs/for-developers/api/websocket/subscriptions]

### Per-asset funding context — field table (`PerpAssetCtx`)

Returned inside `metaAndAssetCtxs` and WS `activeAssetCtx.ctx`.
[`hyperliquid-python-sdk@0.24.0 hyperliquid/utils/types.py:L99-L113`; `info.py:L291-L324`]

| Field         | Type            | Meaning                                          | Citation |
| ------------- | --------------- | ------------------------------------------------ | -------- |
| `funding`     | str→`Decimal`   | Current (hourly) funding rate for the asset      | types.py:L102 |
| `openInterest`| str             | Open interest                                    | types.py:L103 |
| `prevDayPx`   | str             | Price 24h ago                                    | types.py:L104 |
| `dayNtlVlm`   | str             | 24h notional volume                              | types.py:L105 |
| `premium`     | str             | Current premium index                            | types.py:L106 |
| `oraclePx`    | str             | Oracle price (funding notional basis)            | types.py:L107 |
| `markPx`      | str             | Mark price                                       | types.py:L108 |
| `midPx`       | str \| null     | Mid price                                        | types.py:L109 |
| `impactPxs`   | (str,str) \| null | Impact bid/ask prices (drive the premium)      | types.py:L110 |
| `dayBaseVlm`  | str             | 24h base volume                                  | types.py:L111 |

(The docs' WS `PerpsAssetCtx` schema lists a narrower set — `funding, openInterest, oraclePx` plus
shared `dayNtlVlm, prevDayPx, markPx, midPx` — and omits `premium`/`impactPxs`/`dayBaseVlm`. Prefer
the pinned SDK type as the authoritative field list for `0.24.0`.)

---

## Open questions / caveats

- **Referral-discount percentage** — captured the volume caps ("rewards for first $1B, discounts for
  first $25M") but **not** the exact discount %. Live value is `userFees.activeReferralDiscount`.
  **(unverified)**
- **`feeToken == "USDC"` for perps** — currency is verified as USDC (perp collateral); the exact
  `"USDC"` string literal on a perp fill was inferred, not quoted from a fill example. **(unverified
  literal, verified currency)**
- **`userFunding.usdc` sign** — no prose statement in the docs; the sign (negative = paid) is derived
  from the worked example + the positive-rate=long-pays convention. Arithmetically unambiguous but
  **prose-unverified**.
- **`builderFee` on fills** — present in the WS docs (`builderFee?: string`) but **absent** from the
  pinned SDK `Fill` TypedDict; do not rely on SDK typing for it.
- **`nSamples` in the funding record** — present in REST `userFunding.delta` (can be `null`) but
  **absent** from the WS `WsUserFunding` shape per the docs.
- **4%/hour cap** — quoted verbatim from the funding page; not independently cross-checked against a
  worked extreme example.
- **SDK provides no typed model** for `userFunding` records or `predictedFundings`; both come back as
  `Any` / unwrapped at `0.24.0`, so field shapes rely on the docs rather than SDK types.
- **Docs are unversioned** — the GitBook pages have no version pin, so they reflect the live venue at
  the 2026-07-22 fetch date, which may drift from the `0.24.0` SDK.

---

## Sources

**Official docs** (`https://hyperliquid.gitbook.io/hyperliquid-docs/…`), fetched 2026-07-22:

- Fees — `/trading/fees`
- Funding — `/trading/funding`
- Margining — `/trading/margining`
- API · Info endpoint · Perpetuals — `/for-developers/api/info-endpoint/perpetuals`
- API · WebSocket · Subscriptions — `/for-developers/api/websocket/subscriptions`

**SDK source** — `hyperliquid-dex/hyperliquid-python-sdk` at git tag `0.24.0`
(`https://github.com/hyperliquid-dex/hyperliquid-python-sdk/tree/0.24.0`):

- `hyperliquid/utils/types.py` — `Fill` (L132-L155), `PerpAssetCtx` / `ActiveAssetCtx`
  (L99-L116), subscription types (`UserFillsSubscription` L44, `UserFundingsSubscription` L47),
  `BuilderInfo` (L185).
- `hyperliquid/info.py` — `user_fills` (L201-L228), `meta_and_asset_ctxs` (L291-L324),
  `funding_history` (L402-L428), `user_funding_history` (L430-L446), `user_fees` (L506-L547),
  `user_non_funding_ledger_updates` (L652-L668).

**Repo pin** — `pyproject.toml:15` (`hyperliquid-python-sdk~=0.24`); `uv.lock:501-502`
(resolved `0.24.0`).
