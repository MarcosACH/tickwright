# Hyperliquid: how a fill's realized PnL is rounded and credited

**Date:** 2026-10-27.
**For:** [#580](https://github.com/MarcosACH/tickwright/issues/580).
**SDK version:** `hyperliquid-python-sdk == 0.24.0` (`uv.lock:838-839`), read at git tag `0.24.0`.

The engine books realized PnL itself, with an exact average-cost reducer. The reconcile compares
that ledger cash to the venue cash, `accountValue − Σ unrealizedPnl`, after rounding both to
0.000001. The question is whether the venue's cash moves by a per-fill amount rounded to 6
decimals. If it does, many partial closes could build a gap above half a micro-USDC.

> **Status: point-in-time capture.** This note is not maintained. Where it disagrees with an ADR,
> the ADR wins.

## How the evidence was gathered

Hyperliquid documents no rounding rule for PnL or balances. Most findings below are measured, not
quoted. All reads were public and unsigned. No order was placed.

- **Mainnet sample.** 300 random addresses from the public leaderboard
  (`https://stats-data.hyperliquid.xyz/Mainnet/leaderboard`). One `clearinghouseState` each, and
  `userFills` for 37 of the 47 that held perp positions. The other 10 fill reads failed. That gave
  142 open positions and 60,024 perp fills.
- **Replay.** For each coin, the fills were chained by `startPosition` from the last open from
  flat. Each chain was replayed under several candidate rules. A rule counts as a match only when
  it reproduces the venue's number to the last digit.
- **Local evidence.** Testnet fills recorded under `.agents/verify/*/evidence/rt.venue-fills.json`,
  and the recorded snapshots in `tests/venues/hyperliquid/test_account.py`.

Notation used below. `ntl` is the venue's entry notional of an open position, `|szi| × entry`.
`szDecimals` comes from the `meta` response.

## 1. Precision of `closedPnl`

- **Type.** A decimal string. The SDK calls it a "float string"
  ([`info.py:213`](https://github.com/hyperliquid-dex/hyperliquid-python-sdk/blob/0.24.0/hyperliquid/info.py#L213)).
  The websocket `WsFill` types it `string`
  ([subscriptions](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions)).
  The SDK `Fill` TypedDict is shared by `userFills` and the `user` websocket feed
  ([`types.py:132-154`](https://github.com/hyperliquid-dex/hyperliquid-python-sdk/blob/0.24.0/hyperliquid/utils/types.py#L132-L154)).
- **At most 6 decimals for perps.** In the 60,024 mainnet perp fills, no `closedPnl` had more than
  6 decimals. The 7 exceptions were float64 artifacts on values above 1,000, such as
  `-913423.6508769999`. Trailing zeros are dropped, so `0.0191` and `-0.00007` both occur.
- **Spot uses 8 decimals.** Spot fills in the same sample showed values like `0.47628001`. This
  note covers perps only.
- **Not rounded.** `closedPnl` is exact by construction. It is a price difference times a size
  (see §2). The price has at most `6 − szDecimals` decimals
  ([tick and lot size](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/tick-and-lot-size)).
  The size has at most `szDecimals`. So the product has at most 6 decimals and needs no rounding.

Confidence: high on the 6-decimal bound. Medium-high on "exact by construction", since it rests on
the §2 formula.

## 2. How `entryPx` and `closedPnl` are computed

**What the docs say.** The entry price is a size-weighted average on opening trades. It is kept the
same on closing trades. Closed PnL is `fee + side * (mark_price - entry_price) * position_size`
([entry price and pnl](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/entry-price-and-pnl)).
The same page says these numbers "are purely frontend components provided for user convenience".
It adds that "the fundamental accounting is based on margin ... and trades".

**What the venue does, measured.**

- **The displayed `entryPx` is truncated toward zero to `6 − szDecimals` decimals.** The true entry
  is `ntl / |szi|`, recovered as `sign × positionValue − unrealizedPnl`. In 108 of 108 positions
  where `entryPx` differed from the true entry, truncation matched. Rounding half-even matched
  only 57.
- A local example: HYPE short, true entry `57.135957435`, shown as `57.1359`
  (`tests/venues/hyperliquid/test_account.py:210-212`). Rounding would show `57.1360`.
- **`closedPnl` uses the displayed (truncated) entry.** The rule is
  `closedPnl = (px − entryPx_shown) × closed_size × side`. It matched 6,308 of 6,382 closing fills
  (98.8%). The 74 misses cluster in 12 accounts and are unexplained. A replay gap is the likely
  cause, but that is not proven.
- A testnet example: three opens at 76934, 76935, 76936 make a true entry of 76935.19. The close
  of 0.001 at 76916 reads `closedPnl: "-0.0191"`. That is `(76916 − 76935.1) × 0.001`. The exact
  average-cost figure is `-0.01919` (`.agents/verify/testnet-r7/evidence/rt.venue-fills.json:13`).
- **`closedPnl` is gross of fees.** No perp opening fill in the sample had a non-zero `closedPnl`.
  This contradicts the docs formula above. It agrees with the earlier finding in
  `docs/research/hyperliquid-perp-fees-funding.md` (status block, #142/#152).

Confidence: high.

## 3. Does the USDC balance move by `closedPnl − fee`?

**No.** `closedPnl` is not the cash movement. On 3,853 of 5,642 partial closes, it differed from
the cash-based figure below.

**How the venue cash moves, measured and inferred.**

- **The venue keeps `ntl` in micro-USDC.** On a partial close it sets
  `ntl_new = trunc_6(ntl_old × remaining / old_size)`. `trunc_6` is truncation toward zero at
  0.000001.
- Six positions had at least one partial close since opening and a fully replayable history. In 6
  of 6, this rule reproduced the venue's `ntl` exactly. Exact arithmetic, half-even, and half-up
  rounding all failed on every one.

  | coin | partial closes | exact `ntl` | venue `ntl` | drift (USDC) |
  | --- | ---: | ---: | ---: | ---: |
  | XTEN | 2 | 214.337314388… | 214.337313 | 0.0000014 |
  | AERO | 6 | 509.604334288… | 509.604333 | 0.0000013 |
  | XPL short | 13 | 4853.541838701… | 4853.541835 | 0.0000037 |
  | GRASS | 15 | 6491.715545962… | 6491.715541 | 0.0000050 |
  | HYPE short | 326 | 65514.096166765… | 65514.096134 | 0.0000328 |
  | ETH short | 592 | 464213.887735319… | 464213.887718 | 0.0000173 |

- **The venue cash that the reconcile reads is `totalRawUsd + Σ signed ntl`.** This is algebra.
  `accountValue = totalRawUsd + Σ signed positionValue` is a measured identity (ADR-0046 §2,
  #142). `unrealizedPnl = signed positionValue − signed ntl`.
- **`totalRawUsd` moves by the trade cash flow.** That is `−signed(px × sz) − fee` per fill. This is
  inferred from the docs line "accounting is based on margin and trades". It is not observed with a
  before and after snapshot. No such pair exists in this repo.
- Put together, the realized amount the venue credits on a partial close is
  `side × (px × closed − (ntl_old − ntl_new))`. It always has at most 6 decimals.
- At flat, `ntl` is 0. The venue cash is then plain `totalRawUsd`, with no truncation residue.

**Storage unit.**

- Every perp USDC figure in the sample had at most 6 decimals. Values above about 10^6 showed
  float64 artifacts, such as `3547092.3375280001`. Quantizing to 6 decimals removes them.
- The SDK sends perp margin amounts as an integer of micro-USDC
  ([`signing.py:489-490`](https://github.com/hyperliquid-dex/hyperliquid-python-sdk/blob/0.24.0/hyperliquid/utils/signing.py#L489-L490),
  used as `ntli` at
  [`exchange.py:413-418`](https://github.com/hyperliquid-dex/hyperliquid-python-sdk/blob/0.24.0/hyperliquid/exchange.py#L413-L418)).
- Fees in the sample had at most 6 decimals (53,598 of 60,024 had exactly 6).
- No first-party page states "integer micro-USDC". The unit is an inference from these facts.

Confidence: high on the `ntl` truncation rule. Medium on `totalRawUsd` moving by exact trade flows.
Medium-high on micro-USDC storage.

## 4. Per fill, or accumulated and rounded later?

- **The rounding is on the entry notional, at every partial close.** It is truncation toward zero
  at 0.000001. The error carries into the next partial close, scaled by the size ratio. That is why
  drift grows with the number of partial closes in the §3 table.
- Each truncation adds less than 0.000001 of drift. For a long, the venue cash ends lower than the
  exact ledger. For a short, it ends higher.
- **No residue survives a full close.** At flat, venue cash is `totalRawUsd`, which (per §3) moves
  by exact 6-decimal trade flows and fees.
- **`closedPnl` is computed per fill from the truncated `entryPx`.** It is a display figure. It does
  not feed the balance.

Confidence: medium-high. The §3 table is six positions. The mechanism is not documented.

## 5. Evidence in this repo

- **`tests/`.** No recorded fill sequence with balances before and after. The only fill fixture is a
  fake with `closedPnl: "0.0"` (`tests/_support/hyperliquid_fakes.py:199`). The recorded
  `clearinghouseState` bodies carry `entryPx`, `unrealizedPnl`, and `totalRawUsd`.
- **Truncated `entryPx` in a recorded body.** The mainnet `MIXED_SNAPSHOT`
  (`tests/venues/hyperliquid/test_account.py:203-258`) shows it twice:
  - HYPE: `entryPx 57.1359`, true `57.135957435` (lines 210-212).
  - CASHCAT: `entryPx 0.053751`, true `0.053751452781` (lines 230-232).
  The `unrealizedPnl` values use the untruncated entry, with 5 and 6 decimals.
- **Testnet fills.** `.agents/verify/*/evidence/rt.venue-fills.json` holds 27 unique perp fills.
  `closedPnl` has 1 to 5 decimals and `fee` has 5 to 6. One case separates the rules, the BTC
  example in §2. No partial close at an uneven entry is recorded.
- **#580 itself.** The heal logged ledger `297.7839449999999999999999999` against venue
  `297.783945` ([#580](https://github.com/MarcosACH/tickwright/issues/580)). The position was flat
  by then.
  - Suppose the venue had credited `closedPnl` from a truncated ETH entry (2 decimals). Two opens
    of 0.0047 and 0.0004 at different prices would leave a gap of a whole number of micro-USDC.
    That number would be 1 to 50.
  - The observed gap is 1e-25. That fits the exact-trade-flow model and rules out
    `closedPnl` crediting. It assumes the fills were as the issue describes them. They are not
    recorded locally.

## What this means for #580

These are the facts the decision waits on.

- When the account is flat, venue cash equals the exact average-cost ledger to the micro-USDC. The
  #580 gap of 1e-25 is the ledger's own `Decimal` precision, not venue rounding.
- While a position is open after one or more partial closes, venue cash is
  `totalRawUsd + signed trunc_6 ntl`. An exact ledger differs from it by the accumulated truncation
  drift.
- That drift is under 0.000001 per partial close. In the sample it reached 0.0000014 after 2
  partial closes and 0.0000328 after 326. So rounding both sides to 6 decimals can still leave a
  1-micro gap after a single partial close, and more after several.
- The venue's rule is reproducible: truncate `ntl × remaining / old_size` toward zero at 0.000001,
  on each partial close.
- `closedPnl` is not the cash delta. It uses the truncated displayed `entryPx`. It differed from
  the cash-based figure on most partial closes.
- Still unknown:
  - A direct before and after `totalRawUsd` check around one fill.
  - The rounding rule behind `fee`.
  - Whether `ntl` truncation also applies on a flip through zero.

## Testnet check for #582

**Date:** 2026-10-27.

Six BTC fills on testnet: two buys at different prices, two partial closes, a flip, and a close to
flat. Net cash is `equity − Σ signed positionValue` on the venue side. It is the cash line minus
the cost basis of every open position.

| Step | Fill | Venue truncation drift | Net cash gap |
| --- | --- | --- | --- |
| 1-2 | Buys at 80739 and 80740 | 0 | 0 |
| 3 | Partial close, 2 fills | −0.0000013 | 0 |
| 4 | Partial close | −0.0000007 | 0 |
| 5 | Flip to short | 0, entry resets to the fill price | 0 |
| 6 | Close to flat | 0 | 0 |

- `equity − Σ signed positionValue` equaled `totalRawUsd` exactly at every step.
- Net cash moved by exactly `−signed(px × sz) − fee` on every fill.
- The venue truncates on each partial **fill**, not once per order.
- No funding was paid during the run.

This settles the open unknowns above. The `totalRawUsd` check is done. The fee rule and the flip
rule no longer matter, because the reconcile compares net cash and the ledger does not copy the
truncation.
