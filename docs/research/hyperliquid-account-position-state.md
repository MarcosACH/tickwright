# Hyperliquid: account, position, and margin state reported for perpetuals

**Resolves:** wayfinder research ticket #108 (map #107).
**SDK version validated:** `hyperliquid-python-sdk == 0.24.0` (pyproject `~=0.24`; `uv.lock` resolves 0.24.0; installed build confirmed `0.24.0` via `importlib.metadata`). SDK source read at the exact git **tag `0.24.0`** (the repo tags carry **no** `v` prefix).
**Date:** 2026-07-22.
**Scope:** facts only — the exact fields the Tickwright live read/reconcile path can depend on. No design decisions.

> **Status: point-in-time capture, superseded in part — the ADRs are canonical.**
> This note records what the venue's docs and the pinned SDK said on the date above. It is kept
> **verbatim** as the evidence behind ADR-0034 – ADR-0046 and is **not** maintained; where it
> disagrees with an ADR, the ADR wins (`CLAUDE.md` docs-sync policy). Later measured against a live
> venue by [#142](https://github.com/MarcosACH/tickwright/issues/142) and
> [#152](https://github.com/MarcosACH/tickwright/issues/152):
>
> | in this note | current answer |
> | --- | --- |
> | §2 — *"Free / available collateral = `withdrawable` (root)"* | **Superseded.** The venue's rule is `withdrawable = max(0, accountValue − max(total_initial_margin, 0.1 × totalNtlPos))` — the two deductions are **alternatives, not a sum**, the first covers *all* initial margin (positions and resting orders alike), and the second is a withdrawal haircut. It answers *"what could I take off the venue"*, not *"what free collateral do I have"*. Free margin is sourced from `crossMarginSummary.accountValue − crossMarginSummary.totalMarginUsed` — [ADR-0046](../adr/0046-account-abstraction-mode-and-account-grain-sources.md) §2. |
> | Open Q3 — fill `fee` sign, *"negative = maker rebate"* (secondary sources only) | **Answered, and the shorthand misleads.** The sign convention is right, but `crossed: false` does **not** imply a negative fee: a measured maker fill charged the base `+0.015 %` as a *cost*. The rebate branch is gated on a maker-rebate **volume tier** and remains unobserved — [ADR-0036](../adr/0036-perp-fee-model.md). |
> | Open Q2 — `crossMaintenanceMarginUsed` presence | **Observed live** on both networks, repeatedly, in #142/#152. |
>
> All of it now presupposes **Manual/Standard** account-abstraction mode: under `unifiedAccount` the
> perps `clearinghouseState` reports only perps-posted collateral — [ADR-0046](../adr/0046-account-abstraction-mode-and-account-grain-sources.md) §1.

## How to read this document

Every perpetuals read is a `POST /info` HTTP call with a `{"type": ...}` body. The 0.24.0 `Info` class is a **thin wrapper**: `Info` extends `API`, and `API.post` just does `session.post(url, json=payload)` with `Content-Type: application/json` and **no signature / no auth header** ([`api.py:20-28`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/api.py)). So **the Hyperliquid docs own the response schema**; the SDK owns the method name → request `type` mapping and its (sometimes stale) TypedDict shapes.

**Numbers are JSON strings.** Hyperliquid returns essentially every price / size / USDC / rate value as a **quoted string**, not a JSON number. Those are the rows flagged `Decimal? = yes` below (parse to `Decimal`, never `float`). The values that come back as genuine JSON numbers are timestamps (`time`, ms epoch), integer ids (`oid`, `tid`), and a handful of leverage integers — flagged `Decimal? = no`.

Base URLs ([`constants.py`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/constants.py)): mainnet `https://api.hyperliquid.xyz`, testnet `https://api.hyperliquid-testnet.xyz`. The `/info` path is appended by `Info` methods.

### Request-type → 0.24.0 method index

| `POST /info` type | 0.24.0 `Info` method | source |
| --- | --- | --- |
| `clearinghouseState` | `Info.user_state(address, dex="")` | [`info.py:86-128`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/info.py) |
| `metaAndAssetCtxs` | `Info.meta_and_asset_ctxs()` | [`info.py:291-324`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/info.py) |
| `predictedFundings` | **no wrapper in 0.24.0** — call `info.post("/info", {"type": "predictedFundings"})` | (absent from `info.py`) |
| `userFunding` | `Info.user_funding_history(user, startTime, endTime=None)` | [`info.py:430-446`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/info.py) |
| `fundingHistory` | `Info.funding_history(name, startTime, endTime=None)` | [`info.py:402-428`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/info.py) |
| `userFills` | `Info.user_fills(address)` | [`info.py:201-228`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/info.py) |
| `userFillsByTime` | `Info.user_fills_by_time(address, start_time, end_time=None, aggregate_by_time=False)` | [`info.py:230-271`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/info.py) |
| `userNonFundingLedgerUpdates` | `Info.user_non_funding_ledger_updates(user, startTime, endTime=None)` | [`info.py:652-669`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/info.py) |

Docs pages cited below:
- Perpetuals info endpoint (clearinghouseState, metaAndAssetCtxs, predictedFundings, userFunding, fundingHistory): <https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals>
- General info endpoint (userFills, userFillsByTime): <https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint>
- WebSocket subscriptions: <https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions>
- Funding mechanics (interval + sign): <https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding>

---

## 1. Open positions — `clearinghouseState` (`Info.user_state`)

`Info.user_state(address, dex="")` → `POST /info {"type": "clearinghouseState", "user": address, "dex": dex}` ([`info.py:128`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/info.py)). **Full snapshot** of the whole account. `assetPositions` is a list; each entry is `{"type": "oneWay", "position": {...}}`. A coin with no position is simply absent from the list (Hyperliquid runs one-way/net positions, not hedged long+short).

### `assetPositions[].position` fields

| JSON field | type-as-returned | Decimal? | unit | sign | snapshot/delta | source |
| --- | --- | --- | --- | --- | --- | --- |
| `coin` | string | no | symbol name | — | snapshot | [`info.py:99`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/info.py); [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) |
| `szi` | string | **yes** | coin/base units | **signed: `+` = long, `−` = short** | snapshot | [`info.py:110`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/info.py); [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) |
| `entryPx` | string (optional) | **yes** | USDC (price) | unsigned | snapshot | [`info.py:100`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/info.py); [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) |
| `positionValue` | string | **yes** | USDC (notional) | unsigned (abs notional) | snapshot | [`info.py:108`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/info.py); [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) |
| `unrealizedPnl` | string | **yes** | USDC | **signed: `+` = profit** | snapshot | [`info.py:111`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/info.py); [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) |
| `returnOnEquity` | string | **yes** | ratio (1.0 = 100%) | signed | snapshot | [`info.py:109`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/info.py); [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) |
| `marginUsed` | string | **yes** | USDC | unsigned | snapshot | [`info.py:107`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/info.py); [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) |
| `liquidationPx` | string **or null** | **yes** | USDC (price) | unsigned; `null` when none | snapshot | [`info.py:106`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/info.py); [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) |
| `maxLeverage` | **number** | no | integer × | unsigned | snapshot | [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) — **not in the 0.24.0 `user_state` docstring**, see open questions |
| `leverage.type` | string | no | `"cross"` \| `"isolated"` | — | snapshot | [`info.py:102`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/info.py); [`types.py:82-97`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py) |
| `leverage.value` | **number** | no | integer × | unsigned | snapshot | [`info.py:103`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/info.py); [`types.py:82-97`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py) |
| `leverage.rawUsd` | string | **yes** | USDC | signed | snapshot | **isolated only** — present only when `leverage.type == "isolated"` ([`info.py:104`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/info.py); [`types.py:89-95`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py)) |
| `cumFunding.allTime` | string | **yes** | USDC | signed (see open questions) | snapshot | [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) — **not in the 0.24.0 docstring** |
| `cumFunding.sinceOpen` | string | **yes** | USDC | signed | snapshot | [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) |
| `cumFunding.sinceChange` | string | **yes** | USDC | signed | snapshot | [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) |

Note: the 0.24.0 `user_state` docstring ([`info.py:95-127`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/info.py)) is an **incomplete illustration** — it omits `maxLeverage` and `cumFunding`, which the current perpetuals docs list on the position object. The SDK returns `Any` (raw parsed JSON), so the extra fields still arrive; the docstring just doesn't enumerate them. Treat the docs as authoritative for presence.

---

## 2. Account / margin state — `marginSummary`, `crossMarginSummary`, root fields

Same `clearinghouseState` response, root level. `marginSummary` and `crossMarginSummary` are identical `MarginSummary` shapes ([`info.py:121-126`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/info.py)): `marginSummary` covers the whole account (cross **plus** isolated), `crossMarginSummary` covers cross-margined positions only. For a cross-only account they are equal. **Full snapshot.**

| JSON field | type-as-returned | Decimal? | unit | sign | snapshot/delta | source |
| --- | --- | --- | --- | --- | --- | --- |
| `marginSummary.accountValue` | string | **yes** | USDC | signed | snapshot | [`info.py:122`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/info.py); [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) |
| `marginSummary.totalNtlPos` | string | **yes** | USDC (notional) | unsigned (sum of abs) | snapshot | [`info.py:124`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/info.py); [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) |
| `marginSummary.totalRawUsd` | string | **yes** | USDC | signed | snapshot | [`info.py:125`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/info.py); [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) |
| `marginSummary.totalMarginUsed` | string | **yes** | USDC | unsigned | snapshot | [`info.py:123`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/info.py); [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) |
| `crossMarginSummary.accountValue` | string | **yes** | USDC | signed | snapshot | [`info.py:116`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/info.py); [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) |
| `crossMarginSummary.totalNtlPos` | string | **yes** | USDC (notional) | unsigned | snapshot | same as above |
| `crossMarginSummary.totalRawUsd` | string | **yes** | USDC | signed | snapshot | same as above |
| `crossMarginSummary.totalMarginUsed` | string | **yes** | USDC | unsigned | snapshot | same as above |
| `crossMaintenanceMarginUsed` | string | **yes** | USDC | unsigned | snapshot | [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) — **root level, not in the 0.24.0 docstring** |
| `withdrawable` | string | **yes** | USDC | unsigned | snapshot | [`info.py:118`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/info.py); [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) |
| `time` | **number** | no | ms epoch | — | snapshot | [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) — **root level, not in the 0.24.0 docstring** |

**Where the derived quantities actually come from:**
- **Account equity** = `marginSummary.accountValue` (collateral marked-to-market, i.e. inclusive of unrealized PnL). Use `crossMarginSummary.accountValue` for the cross-only equity.
- **Free / available collateral** = `withdrawable` (root). This is what can be withdrawn / the buffer available for new margin.
- **Maintenance margin required** (cross) = `crossMaintenanceMarginUsed` (root). **Initial** margin currently used = `...MarginSummary.totalMarginUsed`. There is **no** per-position maintenance-margin field in this response; maintenance margin is reported only at the cross-account root as `crossMaintenanceMarginUsed`.

---

## 3. Funding

Three distinct reads. **`userFunding` is per-user, per-payment history; `fundingHistory` is per-asset market history; `metaAndAssetCtxs` carries the current funding rate baked into each asset context; `predictedFundings` is the forward-looking rate.**

### 3a. User funding payments — `userFunding` (`Info.user_funding_history`)

`Info.user_funding_history(user, startTime, endTime=None)` → `POST /info {"type": "userFunding", "user", "startTime", ["endTime"]}` ([`info.py:444-446`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/info.py)). `startTime`/`endTime` are **ms epoch**, inclusive; `endTime` defaults to now. Returns a **list of per-payment records** (each an incremental ledger event, not a snapshot). Verbatim example from docs:

```json
{
    "delta": {
        "coin": "ETH",
        "fundingRate": "0.0000417",
        "szi": "49.1477",
        "type": "funding",
        "usdc": "-3.625312",
        "nSamples": null
    },
    "hash": "0xa166e3fa63c25663024b03f2e0da011a00307e4017465df020210d3d432e7cb8",
    "time": 1681222254710
}
```

| JSON field | type-as-returned | Decimal? | unit | sign | snapshot/delta | source |
| --- | --- | --- | --- | --- | --- | --- |
| `time` | **number** | no | ms epoch | — | delta (per payment) | [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) |
| `hash` | string | no | tx hash | — | delta | [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) |
| `delta.type` | string (`"funding"`) | no | — | — | delta | [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) |
| `delta.coin` | string | no | symbol | — | delta | [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) |
| `delta.usdc` | string | **yes** | USDC | **signed: `−` = user paid funding, `+` = user received** | delta | [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) |
| `delta.szi` | string | **yes** | coin/base units | signed (`+` long, `−` short at payment time) | delta | [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) |
| `delta.fundingRate` | string | **yes** | ratio (per hour) | signed | delta | [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) |
| `delta.nSamples` | number **or null** | no | count | — | delta | [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) (observed `null`) |

Sign of `usdc`: in the example the user is **long** (`szi = +49.1477`) with a **positive** `fundingRate`, and `usdc = -3.625312` — consistent with the sign convention below (positive rate ⇒ longs pay). So a **negative `usdc` = the user paid**.

### 3b. Market funding history — `fundingHistory` (`Info.funding_history`)

`Info.funding_history(name, startTime, endTime=None)` → `POST /info {"type": "fundingHistory", "coin", "startTime", ["endTime"]}` ([`info.py:424-428`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/info.py)). `name` is mapped to the internal coin via `name_to_coin`. Returns a **list of hourly records** per asset. Verbatim example:

```json
{
    "coin": "ETH",
    "fundingRate": "-0.00022196",
    "premium": "-0.00052196",
    "time": 1683849600076
}
```

| JSON field | type-as-returned | Decimal? | unit | sign | snapshot/delta | source |
| --- | --- | --- | --- | --- | --- | --- |
| `coin` | string | no | symbol | — | delta (per hour) | [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) |
| `fundingRate` | string | **yes** | ratio (per hour) | signed | delta | [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) |
| `premium` | string | **yes** | ratio | signed | delta | [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) |
| `time` | **number** | no | ms epoch | — | delta | [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) |

### 3c. Current funding rate per asset — `metaAndAssetCtxs` (`Info.meta_and_asset_ctxs`)

`Info.meta_and_asset_ctxs()` → `POST /info {"type": "metaAndAssetCtxs"}` ([`info.py:324`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/info.py)). Returns a 2-element array: `[ {universe:[...]}, [assetCtx, ...] ]`, positionally aligned — `assetCtx[i]` corresponds to `universe[i]`. **Full snapshot** of all assets. The per-asset context (`PerpAssetCtx`, [`types.py:99-113`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py)):

| JSON field | type-as-returned | Decimal? | unit | sign | snapshot/delta | source |
| --- | --- | --- | --- | --- | --- | --- |
| `funding` | string | **yes** | ratio (**current hourly rate**) | signed | snapshot | [`types.py:102`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py); [`info.py:315`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/info.py); [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) |
| `markPx` | string | **yes** | USDC | unsigned | snapshot | [`types.py:108`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py); [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) |
| `oraclePx` | string | **yes** | USDC | unsigned | snapshot | [`types.py:107`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py); [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) |
| `midPx` | string **or null** | **yes** | USDC | unsigned | snapshot | [`types.py:109`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py); [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) |
| `premium` | string **or null** | **yes** | ratio | signed | snapshot | [`types.py:106`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py); [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) |
| `openInterest` | string | **yes** | coin/base units | unsigned | snapshot | [`types.py:103`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py); [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) |
| `impactPxs` | array[string] **or null** | **yes** | USDC | unsigned | snapshot | [`types.py:110`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py); [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) |
| `dayNtlVlm` | string | **yes** | USDC | unsigned | snapshot | [`types.py:105`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py) |
| `prevDayPx` | string | **yes** | USDC | unsigned | snapshot | [`types.py:104`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py) |

### 3d. Predicted funding — `predictedFundings` (no 0.24.0 wrapper)

**Not wrapped by the 0.24.0 SDK** — there is no `predicted_fundings`/`predictedFundings` method in `info.py` at tag 0.24.0. Call it raw: `info.post("/info", {"type": "predictedFundings"})`. Docs shape: `coin → [ [venueName, {fundingRate, nextFundingTime}], ... ]` where `fundingRate` is a **string** (ratio) and `nextFundingTime` is a **number** (ms epoch) ([docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals)). Exact nesting couldn't be fully pinned from the SDK — see open questions.

### Funding interval & sign convention (authoritative)

From the funding mechanics page (<https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding>), quoted verbatim:
- **Interval:** *"The funding rate on Hyperliquid is paid every hour."* (Hourly — not the 8h common elsewhere.)
- **Sign:** *"If the contract's price is higher than the oracle price, the premium and hence the funding rate will be positive, and the long position will pay the short position."* ⇒ **`fundingRate > 0` ⟹ longs pay shorts; `fundingRate < 0` ⟹ shorts pay longs.**
- **Interest-rate component:** *"the interest rate component is predetermined at 0.01% every 8 hours, which is 0.00125% every hour"*; formula *"Funding Rate (F) = Average Premium Index (P) + clamp(interest rate − Premium Index (P), −0.0005, 0.0005)"*.
- **Application:** *"The funding rate is added or subtracted from the balance of contract holders at the funding interval"*; the payment *"is `position_size * oracle_price * funding_rate`"* (note: computed on **oracle** price).

---

## 4. Fills — `userFills` / `userFillsByTime`

`Info.user_fills(address)` → `{"type": "userFills", "user"}` ([`info.py:228`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/info.py)); `Info.user_fills_by_time(address, start_time, end_time=None, aggregate_by_time=False)` → `{"type": "userFillsByTime", "user", "startTime", "endTime", "aggregateByTime"}` ([`info.py:262-271`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/info.py)). Both return a **list of fills** (each an incremental event). `userFills` returns **at most the 2000 most recent**; `userFillsByTime` pages by `startTime`/`endTime` (ms epoch), at most 2000 per response, only the 10000 most recent available ([docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint)). Verbatim docs example (a perp fill):

```json
{
    "closedPnl": "0.0",
    "coin": "AVAX",
    "crossed": false,
    "dir": "Open Long",
    "hash": "0xa166e3fa63c25663024b03f2e0da011a00307e4017465df020210d3d432e7cb8",
    "oid": 90542681,
    "px": "18.435",
    "side": "B",
    "startPosition": "26.86",
    "sz": "93.53",
    "time": 1681222254710,
    "fee": "0.01",        // the total fee, inclusive of builderFee below
    "feeToken": "USDC",
    "builderFee": "0.01", // this is optional and will not be present if 0
    "tid": 118906512037719
}
```

| JSON field | type-as-returned | Decimal? | unit | sign | snapshot/delta | source |
| --- | --- | --- | --- | --- | --- | --- |
| `coin` | string | no | symbol | — | delta | [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint); [`types.py:135`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py) |
| `px` | string | **yes** | USDC (fill price) | unsigned | delta | [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint); [`types.py:136`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py) |
| `sz` | string | **yes** | coin/base units | unsigned (magnitude; direction is `side`/`dir`) | delta | [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint); [`types.py:137`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py) |
| `side` | string | no | `"B"` = buy/bid, `"A"` = sell/ask | — | delta | [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint); [`types.py:138`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py) |
| `time` | **number** | no | ms epoch | — | delta | [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint); [`types.py:139`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py) |
| `startPosition` | string | **yes** | coin/base units | **signed** (position before this fill; `+` long, `−` short) | delta | [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint); [`types.py:140`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py) |
| `dir` | string | no | display label, e.g. `"Open Long"`, `"Close Short"`, `"Sell"` | — | delta | [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint); [`types.py:141`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py) |
| `closedPnl` | string | **yes** | USDC | **signed: `+` = realized profit on the closed portion** | delta | [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint); [`types.py:142`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py) |
| `hash` | string | no | L1 tx hash | — | delta | [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint); [`types.py:143`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py) |
| `oid` | **number** | no | order id | — | delta | [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint); [`types.py:144`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py) |
| `crossed` | **boolean** | no | taker flag: `true` = this fill crossed the spread (**taker**), `false` = rested (**maker**) | — | delta | [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint); [`types.py:145`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py) |
| `fee` | string | **yes** | USDC (or `feeToken`) | **signed: `−` = maker rebate received** (secondary source; see open q.) — total fee, inclusive of `builderFee` | delta | [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint); [`types.py:146`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py) |
| `tid` | **number** | no | unique trade id | — | delta | [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint); [`types.py:147`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py) |
| `feeToken` | string | no | token the fee is denominated in (e.g. `"USDC"`) | — | delta | [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint); [`types.py:148`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py) |
| `builderFee` | string | **yes** | USDC | unsigned | delta | **optional** (absent if 0) — [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint). **NOT in the 0.24.0 `Fill` TypedDict** ([`types.py:132-149`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py)) |
| `liquidation` | object | — | — | present only on forced-closure fills | delta | **not in the official example and not in the 0.24.0 `Fill` TypedDict** — see open questions |

Note: taker/maker is `crossed` (boolean), **not** `dir`. `dir` is a human-readable display label ("Open Long" / "Close Short" / "Sell"), not a machine enum. The 0.24.0 `Fill` TypedDict ([`types.py:132-149`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py)) enumerates coin, px, sz, side, time, startPosition, dir, closedPnl, hash, oid, crossed, fee, tid, feeToken — it is **missing `builderFee` and `liquidation`**, both of which the live API can return, so parse defensively (`.get(...)`).

---

## WebSocket equivalents (push the same state)

WS subscriptions are declared by the same 0.24.0 SDK via `Info.subscribe(subscription, callback)` ([`info.py:775-780`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/info.py)). The **authoritative list of what the pinned SDK supports** is the `Subscription` union ([`types.py:39-71`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py)):

| Subscription `type` (0.24.0) | Carries | Snapshot vs incremental | source |
| --- | --- | --- | --- |
| `webData2` (`{type, user}`) | Aggregate account/web state for `user`. Channel `"webData2"`, `data: Any` (untyped in SDK). | Snapshot-style push | [`types.py:51`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py), [`types.py:156-168`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py) |
| `activeAssetData` (`{type, user, coin}`) | Per-(user, coin) perp state: `leverage`, `maxTradeSzs`, `availableToTrade`, `markPx` **(per the 0.24.0 TypedDict)**. Channel `"activeAssetData"`. | Push on change | [`types.py:53-55`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py), [`types.py:120-131`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py) |
| `userEvents` (`{type, user}`) | Channel name is **`"user"`**; `data` may contain `fills: List[Fill]` (also funding / liquidation / cancels). | Incremental events | [`types.py:43`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py), [`types.py:151-153`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py) |
| `userFills` (`{type, user}`) | `UserFillsData = {user, isSnapshot: bool, fills: List[Fill]}` on channel `"userFills"`. First message `isSnapshot=true`, then `isSnapshot=false` deltas. | **Snapshot then incremental** (see `isSnapshot`) | [`types.py:44`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py), [`types.py:154-155`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py); [WS docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions) |
| `userFundings` (`{type, user}`) | Channel `"userFundings"`, `data: Any`. Pushes funding-payment snapshot then hourly updates carrying `time, coin, usdc, szi, fundingRate` (same shape as §3a `delta`). | Snapshot then hourly incremental | [`types.py:47`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py), [`types.py:156-168`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py); [WS docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions) |
| `userNonFundingLedgerUpdates` (`{type, user}`) | Deposits, withdrawals, transfers, liquidations, etc. Channel same name, `data: Any`. | Incremental events | [`types.py:48-50`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py) |
| `activeAssetCtx` (`{type, coin}`) | Per-asset `PerpAssetCtx` (funding/markPx/oraclePx/…) — the §3c fields, pushed live. | Push on change | [`types.py:52`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py), [`types.py:99-116`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py) |
| `allMids`, `bbo`, `l2Book`, `trades`, `candle`, `orderUpdates` | Market data / order updates (not account/margin state). | — | [`types.py:39-46`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py) |

**Version-drift warning (server ahead of SDK 0.24.0):** the live WS subscriptions doc (fetched 2026-07-22) now advertises **`webData3`** (not `webData2`), a standalone **`clearinghouseState`** subscription, `allDexsClearinghouseState`, `twapStates`, `fastAssetCtxs`, and more that the 0.24.0 SDK does **not** model, and it states **`activeAssetData` no longer includes `markPx`** (moved to `activeAssetCtx`) — yet the 0.24.0 `ActiveAssetData` TypedDict still lists `markPx` ([`types.py:129`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py)). Treat the 0.24.0 union as the ceiling of what the pinned SDK can subscribe to; treat `data` payloads as `Any` and parse defensively.

---

## Auth: read-only (unsigned) vs signed

**Every read in this document is unauthenticated / read-only by address.** All go through `API.post`, which sends only `Content-Type: application/json` and **no signature, no API key, no auth header** ([`api.py:12-28`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/api.py)) — you pass the target address in the request body and get its public account/position/margin state back. This covers `clearinghouseState`, `metaAndAssetCtxs`, `predictedFundings`, `userFunding`, `fundingHistory`, `userFills`, `userFillsByTime`, `userNonFundingLedgerUpdates`, and all the WS `user*` subscriptions (they take a `user` address, not a signature). Signing is only required for the **`Exchange`** side (placing/cancelling orders, updating leverage) — a separate class, out of scope for the read/reconcile path.

---

## Open questions / couldn't confirm at 0.24.0

1. **`cumFunding` sign convention.** The perpetuals docs list `cumFunding.{allTime,sinceOpen,sinceChange}` as strings but **do not state the sign** (whether a positive value means funding *paid* or *received*). By analogy with `userFunding.delta.usdc` (negative = paid) the likely convention is "positive = net funding paid by the position," but I could not pin this to a primary source. `cumFunding` is also **absent from the 0.24.0 `user_state` docstring** — its presence rests on the live docs only.
2. **`maxLeverage` / `crossMaintenanceMarginUsed` / root `time`** on `clearinghouseState` are documented on the live perpetuals page but are **not enumerated in the 0.24.0 SDK docstring**. Presence is well-supported by the docs; exact typing (`maxLeverage` as number, `time` as ms number) is from the docs, not the SDK types.
3. **Fill `fee` sign ("negative = maker rebate").** The 0.24.0 `Fill` TypedDict types `fee` as `str` but says nothing about sign; the official `userFills` example shows only a positive fee and calls it "the total fee." The "negative means rebate" convention comes from **secondary sources** (Go SDK field comments surfaced via web search: <https://pkg.go.dev/github.com/slicken/go_hyperliquid>) — **not confirmed on a Hyperliquid first-party page**. Verify empirically on testnet before relying on the sign.
4. **Fill `liquidation` field.** Not present in the official `userFills` example and **not in the 0.24.0 `Fill` TypedDict**. Secondary sources (QuickNode/Chainstack/Dwellir docs) report an optional `liquidation` object on forced-closure fills. Exact field shape unconfirmed at a first-party source / 0.24.0 — parse defensively.
5. **`predictedFundings` exact JSON nesting.** No 0.24.0 wrapper, so no SDK TypedDict to anchor it. The docs describe `coin → venue-array with fundingRate (string) + nextFundingTime (number ms)`, but the precise array-vs-object nesting could not be verified against the pinned SDK.
6. **`userFunding.delta.nSamples`** was observed as `null` in the docs example; whether it is ever a populated integer for perp funding (vs always null) is not stated.
7. **`webData2` payload shape.** The 0.24.0 SDK types its WS `data` as `Any` ([`types.py:156-168`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/0.24.0/hyperliquid/utils/types.py)); there is no SDK TypedDict for the `webData2` body, and the live docs have moved to `webData3`. If the reconcile path wants to consume `webData2`, the exact fields must be captured empirically at runtime.
