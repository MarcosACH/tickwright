# Hyperliquid Perpetual Fees & Funding Mechanics

**Scope & retrieval date:** Reference facts on Hyperliquid **perpetual** trading fees and funding, gathered from the official Hyperliquid docs and the pinned `hyperliquid-python-sdk` **v0.24.0** on **2026-07-22**. This is a facts-only reference for a *future* fee/funding model; it recommends no design. Tickwright v1 deliberately has no fees/margin/PnL in the engine (ADR-0013). **Fee tiers, the interest-rate constant, and the funding cap are venue parameters that can change over time — every number below is time-sensitive and must be re-verified before it is relied on.**

## Sources

Official docs (Hyperliquid GitBook, retrieved 2026-07-22):

- Fees — https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees
- Funding — https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding
- Builder codes — https://hyperliquid.gitbook.io/hyperliquid-docs/trading/builder-codes
- Info endpoint (general, incl. `userFills`) — https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint
- Info endpoint — Perpetuals (`fundingHistory`, `userFunding`, `metaAndAssetCtxs`, `predictedFundings`) — https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals

Pinned SDK (this repo depends on `hyperliquid-python-sdk~=0.24`; `uv.lock` resolves **0.24.0**, `pyproject.toml:15`, `uv.lock:501-513`):

- `hyperliquid-dex/hyperliquid-python-sdk` `hyperliquid/info.py` @ tag `0.24.0` — https://github.com/hyperliquid-dex/hyperliquid-python-sdk/blob/0.24.0/hyperliquid/info.py
- `hyperliquid-dex/hyperliquid-python-sdk` `hyperliquid/api.py` @ tag `0.24.0` — https://github.com/hyperliquid-dex/hyperliquid-python-sdk/blob/0.24.0/hyperliquid/api.py
- The GitHub tag is `0.24.0` (**no `v` prefix**; `refs/tags/0.24.0`).

**SDK shape:** every `Info` method below is a **thin wrapper over `POST /info`** with a JSON body `{"type": <string>, ...}`; the SDK returns the raw decoded JSON (`Any`) and does not model the response. `Api.post` builds `self.base_url + "/info"` and posts the payload (`api.py:20-23`), with `base_url` defaulting to `MAINNET_API_URL` (`api.py:7,13-14`). So the wrapper method → `type` string mapping *is* the endpoint contract; response field names come from the **docs**, not the SDK (the SDK docstrings are partial — see the `user_fills` caveat under Fees).

---

## Fees

### Perp maker/taker rates and the volume-tier schedule

The perps fee schedule is tiered on **14-day weighted (rolling) volume**. Rates are expressed as percentages of notional ([fees docs](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees)):

| Tier | 14-day volume threshold | Taker | Maker |
|-----:|-------------------------|-------|-------|
| 0 (base) | — | 0.045% | 0.015% |
| 1 | > $5M | 0.040% | 0.012% |
| 2 | > $25M | 0.035% | 0.008% |
| 3 | > $100M | 0.030% | 0.004% |
| 4 | > $500M | 0.028% | 0.000% |
| 5 | > $2B | 0.026% | 0.000% |
| 6 | > $7B | 0.024% | 0.000% |

- Base **taker = 0.045% (4.5 bps)**, base **maker = 0.015% (1.5 bps)** ([fees docs](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees)). At tier 0 the maker column is a **fee the maker pays** (positive), not a rebate; the maker fee only reaches 0.000% at tier 4+.
- **Fee destination:** "On Hyperliquid, fees are entirely directed to the community (HLP, the assistance fund, and deployers)." ([fees docs](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees)) — there is no exchange-operator take.

### Maker rebates (separate schedule)

Above the base schedule there is a **separate maker-rebate schedule** keyed on the maker's **share of 14-day weighted maker volume**, paying the maker a **negative fee** ([fees docs](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees)):

| Maker-volume-share cutoff | Maker fee (rebate) |
|---------------------------|--------------------|
| > 0.5% | -0.001% |
| > 1.5% | -0.002% |
| > 3.0% | -0.003% |

"Maker rebates are paid out continuously on each trade directly to the trading wallet." The negative sign means these are **paid to** the maker ([fees docs](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees)).

### Discounts

- **Staking discounts:** tiered **5% to 40%** off fees, based on HYPE staked (thresholds from > 10 to > 500,000 tokens) ([fees docs](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees)).
- **Referral:** "Referral rewards apply for a user's first $1B in volume and referral discounts apply for a user's first $25M in volume." The **exact referral discount / reward percentages were not stated on the fees page** as retrieved — see Open questions ([fees docs](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees)).

### Builder fees

Builder codes let a front-end/builder attach an extra fee on top of the exchange fee ([builder-codes docs](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/builder-codes)):

- **Max builder fee: 0.1% on perps** (1% on spot).
- "Collected in the quote or collateral asset" and "apply to both sides of perp trades."
- "The user must approve a maximum builder fee for each builder, and can revoke permissions at any time."
- In a fill, the builder fee is reported by the `builderFee` field and is **included within** the total `fee` (see below).

### How maker vs taker is determined, and whether it's reported per fill

- Each fill carries a **`crossed` boolean** ([info-endpoint docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint); present in SDK docstring `info.py:215`). A `crossed`/aggressor fill is the **taker** side; a resting fill is the **maker** side. **Caveat:** the docs show `"crossed": true`/`false` in examples but, as retrieved, **do not spell out in words that `crossed` == taker/maker** — treat the maker/taker read of `crossed` as strongly implied but not verbatim-documented (see Open questions).
- The realized fee amount itself distinguishes the two economically: a taker fill's `fee` is positive (taker rate); a maker fill's `fee` is the maker rate (positive at low tiers, negative when a rebate applies).

### Where the per-fill fee is reported

Per-fill fee fields in a `userFills` / `userFillsByTime` entry ([info-endpoint docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint)):

| Field | Meaning |
|-------|---------|
| `fee` | "the total fee, inclusive of `builderFee`" for that fill |
| `feeToken` | the token the fee is denominated in (example value **`"USDC"`** for perps) |
| `builderFee` | builder fee portion; "optional and will not be present if 0" |
| `crossed` | taker (aggressor) vs maker indicator (see caveat above) |
| `closedPnl` | realized PnL on the fill (separate from `fee`) |

- **Settlement currency:** the fee is denominated by `feeToken`, which is **`USDC`** for perps in the docs example (perp collateral is USDC). **Note:** the *fees* page itself did not state the currency in words as retrieved; the USDC fact comes from the `feeToken` field example on the info-endpoint page ([info-endpoint docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint)).
- **Sign convention:** `fee` is a **positive number for a fee charged** (it reduces balance); a **negative `fee` is a rebate credited** to the wallet ([fees docs](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees), maker-rebate wording).
- **SDK caveat (v0.24.0):** `Info.user_fills` returns raw JSON (`Any`) and posts `{"type": "userFills", "user": address}` (`info.py:201,228`), but its **docstring lists only** `closedPnl, coin, crossed, dir, hash, oid, px, side, startPosition, sz, time` (`info.py:211-224`) and **omits `fee`, `feeToken`, `builderFee`, `tid`**. Those fields are still present in the live response (the wrapper doesn't strip them); the SDK docstring is simply incomplete — trust the docs' schema, not the docstring.

### Account-level fee/tier query — `user_fees`

`Info.user_fees(address)` → `POST /info {"type": "userFees", "user": address}` (`info.py:506,547`) returns the caller's current fee state, whose docstring (`info.py:512-545`) names: `userCrossRate` (effective taker rate), `userAddRate` (effective maker rate), `activeReferralDiscount`, `dailyUserVlm[]`, and `feeSchedule` with `{add, cross, referralDiscount, tiers:{mm:[{add, makerFractionCutoff}], vip:[{add, cross, ntlCutoff}]}}`. Here `add` = maker, `cross` = taker; `tiers.vip` is the volume-tier schedule and `tiers.mm` is the maker-rebate schedule.

---

## Funding

### Cadence and where it comes from

- **Funding is paid every hour** ([funding docs](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding)).
- The published funding rate uses an **8-hour basis**; the hourly charge is **one-eighth of the computed 8-hour rate**: "funding is paid every hour at one eighth of the computed rate for each hour." ([funding docs](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding))
- The **premium is sampled every 5 seconds and averaged over the hour** ([funding docs](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding)); `nSamples` on a `userFunding` entry is the number of samples that fed the interval's rate.
- **Timestamp boundary:** the docs say funding is charged "at the funding interval" / "every hour" but, **as retrieved, do not state the exact UTC boundary** (e.g. top of every hour UTC). Treat the precise charge timestamp as unconfirmed (see Open questions). The per-payment timestamp is available as `time` (ms) on each `userFunding` entry.

### The funding formula

The 8-hour funding rate ([funding docs](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding)):

```
Funding Rate (F) = Average Premium Index (P) + clamp(interest rate − P, −0.0005, 0.0005)
```

- **Premium:** `premium = impact_price_difference / oracle_price`, where
  `impact_price_difference = max(impact_bid_px − oracle_px, 0) − max(oracle_px − impact_ask_px, 0)`
  (for HIP-3 perps: `premium = (0.5 * (impact_bid_px + impact_ask_px) / oracle_px) − 1`) ([funding docs](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding)).
- **Interest-rate component:** "predetermined at 0.01% every 8 hours, which is 0.00125% every hour, or 11.6% APR **paid to short**." ([funding docs](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding))
- **Clamp:** the `clamp(interest rate − P, −0.0005, 0.0005)` term bounds the interest contribution to **±0.05%** (on the 8-hour basis) ([funding docs](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding)).
- **Cap:** "Funding on Hyperliquid is capped at **4%/hour**." ([funding docs](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding))

### Notional basis and payment amount

- **Payment = `position_size * oracle_price * funding_rate`.** "In particular, the **spot oracle price** is used to convert the position size to notional value, **not the mark price**." ([funding docs](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding))

### Sign convention

- For a **positive** funding rate, **longs pay shorts**: "If the contract's price is higher than the oracle price, the premium and hence the funding rate will be positive, and the **long position will pay the short position**." ([funding docs](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding)) (A negative rate flips it: shorts pay longs.)
- On a `userFunding` entry, a **negative `delta.usdc`** is a charge to the account (docs example `"usdc": "-3.625312"`) and a positive value is a credit ([perpetuals info-endpoint docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals)).

### How funding is reported / affects the account

Funding is a **distinct ledger category**, tracked separately from trade PnL and from other ledger updates. The dedicated `userNonFundingLedgerUpdates` endpoint's very name **excludes** funding, confirming funding is its own category (`info.py:652`, `{"type": "userNonFundingLedgerUpdates", ...}`). It adjusts the account's USDC balance ("added or subtracted from the balance of contract holders at the funding interval", [funding docs](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding)) but is **not** part of a fill's `closedPnl`.

Endpoint → field mapping (SDK v0.24.0 wrappers → `type` string → doc-schema fields):

| Purpose | SDK method (`info.py`) | `type` payload | Key response fields (per docs) |
|---------|------------------------|----------------|--------------------------------|
| Per-user funding payments | `user_funding_history(user, startTime, endTime)` (`:430`, `:444-446`) | `userFunding` | `delta{coin, fundingRate, szi, type:"funding", usdc, nSamples}`, `hash`, `time` (ms) |
| Market funding history (per coin) | `funding_history(name, startTime, endTime)` (`:402`, `:423-428`) | `fundingHistory` | `coin`, `fundingRate`, `premium`, `time` (ms) |
| Current per-asset context | `meta_and_asset_ctxs()` (`:291`, `:324`) | `metaAndAssetCtxs` | asset ctx: `funding`, `premium`, `oraclePx`, `markPx`, `midPx`, `impactPxs`, `openInterest`, `dayNtlVlm`, `prevDayPx` |
| Predicted next funding | **not wrapped in SDK 0.24.0** | `predictedFundings` | per-venue `fundingRate`, `nextFundingTime` (ms) |
| Non-funding ledger (funding excluded) | `user_non_funding_ledger_updates(user, startTime, endTime)` (`:652`) | `userNonFundingLedgerUpdates` | ledger deltas (deposits, transfers, etc.) — funding intentionally not here |

**Naming gotcha:** the SDK method is `user_funding_history(...)` but the wire `type` it sends is **`userFunding`** (not `userFundingHistory`) (`info.py:444-446`).

**`predictedFundings` is NOT wrapped by the SDK at 0.24.0** — there is no `predicted_fundings` (or similar) method on `Info` in `info.py`. To use it a caller must `post("/info", {"type": "predictedFundings"})` directly. Its documented shape is per-venue entries `{fundingRate, nextFundingTime}` ([perpetuals info-endpoint docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals)).

- **`fundingRate` in `fundingHistory` / `metaAndAssetCtxs.funding` is the hourly rate** (the value actually applied for that hour = 1/8 of the 8-hour formula rate). Example `metaAndAssetCtxs` `"funding": "0.0000125"` matches the hourly interest floor (0.00125% = 0.0000125) ([perpetuals info-endpoint docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals)). Confirm the interval basis of `fundingRate` against a live sample before relying on it — see Open questions.

---

## Open questions / unverified

Items I could **not** confirm verbatim against a primary source at the pinned version:

1. **Exact funding charge timestamp / UTC boundary.** Docs say "every hour" but do not state the precise boundary (e.g. top of each hour UTC). Confirm from a live `userFunding.time` sample.
2. **`crossed` == taker/maker, in words.** The `crossed` boolean is present per fill (docs + SDK docstring `info.py:215`) and is the natural taker/maker discriminator, but the retrieved docs do not state that mapping in prose. Verify against a live fill where maker/taker is known.
3. **Fee currency stated on the *fees* page.** USDC settlement is established from the `feeToken` example on the info-endpoint page and from perps collateral being USDC, **not** from explicit wording on the fees page.
4. **Exact referral discount / referrer reward percentages.** The fees page gave only volume ranges ("first $1B" rewards, "first $25M" discounts), not the percentage rates.
5. **Interval basis of `fundingRate`/`funding` fields (hourly vs 8-hour).** Inferred to be the **hourly** applied rate from an example value, but not stated verbatim; confirm against a live sample.
6. **`predictedFundings` full multi-venue schema.** The SDK does not wrap it at 0.24.0, so it is documented from the docs only, not exercised through the pinned SDK.
7. **Whether the 8-hour formula's `interest rate` in `clamp(interest rate − P, ...)` uses the 8-hour 0.01% figure inside the clamp** — the docs present the formula and the constants but do not restate the constant *inside* the clamp expression; the ±0.0005 clamp bounds are explicit.

**All fee tiers, the interest-rate constant, the ±0.0005 clamp, and the 4%/hour cap are venue parameters subject to change; re-verify against the live docs and a live API sample before any model depends on them.**
