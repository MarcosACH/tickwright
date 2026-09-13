# Hyperliquid: how long `orderStatus` keeps an order, versus how long fills stay

**Research date:** 2026-09-13
**Pinned SDK:** `hyperliquid-python-sdk==0.24.0` (`pyproject.toml` `~=0.24`, `uv.lock` resolves
`0.24.0`). SDK source cited at git tag `0.24.0`.
**Feeds:** issue [#242](https://github.com/MarcosACH/tickwright/issues/242).

Scope: facts only. This note does not choose a fix.

Citation convention:

- Docs: a URL under `https://hyperliquid.gitbook.io/hyperliquid-docs`.
- SDK: `hyperliquid-python-sdk@0.24.0 <path>:L<n>`.
- Probe: a row observed on mainnet on the research date. The script is in the method section.
- A claim with no citation is marked **(unverified)**.

## The question

Can `orderStatus` answer `{"status": "unknownOid"}` for an order whose fill still shows in
`userFills` or `userFillsByTime`? The adapter asks by cloid, not oid
(`src/tickwright/venues/hyperliquid/exchange.py:513-514`). So the answer must hold for both keys.

## Short answer

**Yes. Confidence: high.**

The venue keeps an order record for roughly the last 2000 orders of the account, by count. Fills
are kept far longer, at least 980 days. Once an account places about 2000 more orders, the record
of an older order is gone while its fill stays. The two keys share one record. Lookup by cloid
fails exactly when lookup by oid fails.

Confidence is high because the probe observed 188 such orders across 15 of 28 mainnet accounts,
with fill ages from 7 minutes to 830 days, and no counter-example.

## Documented facts

| Fact | Source |
| --- | --- |
| `orderStatus` takes `oid` as "either u64 representing the order id or 16-byte hex string representing the client order id". | [info-endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint) |
| A missing order answers `{"status": "unknownOid"}`. No retention policy is stated for `orderStatus`. | same page |
| `historicalOrders` returns "at most 2000 most recent historical orders". | same page |
| `userFills` returns "at most 2000 most recent fills". | same page |
| `userFillsByTime` returns "at most 2000 fills per response" and "only the 10000 most recent fills are available". Page with the last timestamp as the next `startTime`. | same page |
| The docs example fill row has no `cloid` field. The `historicalOrders` and `orderStatus` example rows do carry `cloid`. | same page |
| No page states a time limit for lookup by cloid. The perpetuals sub-page says nothing about retention. | [info-endpoint/perpetuals](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) |
| REST weight limit is 1200 per minute per IP. `orderStatus` weighs 2. `userFills`, `userFillsByTime`, `historicalOrders`, `recentTrades` weigh 20 plus 1 per 20 rows returned. | [rate-limits-and-user-limits](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits) |
| SDK `query_order_by_oid` and `query_order_by_cloid` both send `{"type": "orderStatus", "user", "oid"}`. The cloid goes in the `oid` field as raw hex. | `hyperliquid-python-sdk@0.24.0 hyperliquid/info.py:L613-617` |
| SDK `historical_orders` sends `{"type": "historicalOrders", "user"}`. | `hyperliquid/info.py:L637-650` |
| SDK `user_fills` and `user_fills_by_time` docstrings list no `cloid` on a fill row. | `hyperliquid/info.py:L201-272` |

## Probe method

All reads are unsigned `POST https://api.hyperliquid.xyz/info`. Nothing was placed or signed.

1. Collect addresses. `recentTrades` for 36 coins gives a `users` pair per trade. The
   leaderboard at `https://stats-data.hyperliquid.xyz/Mainnet/leaderboard` (45,082 rows) gives
   the top 10, 10 from the middle, and 10 from the tail. Plus the HLP vault.
2. Per address: `userFills` (newest 2000), `userFillsByTime` with `startTime: 0` (oldest
   2000), `historicalOrders`, `openOrders`.
3. Pick about 20 fill oids per address: the 2 oldest, the 2 newest, 12 evenly spread, and 6
   around the oldest `historicalOrders` timestamp. Ask `orderStatus` by oid for each.
4. For 4 `historicalOrders` rows with a cloid, ask by cloid and by oid and compare.
5. For orders that answered `unknownOid` and whose fill row carries a cloid, ask by cloid too.
6. Throttle to 900 weight per rolling minute. About 500 weight per address.

Core of the script (full version was `/tmp/hl_probe.py`, rerunnable as written):

```python
def probe(addr):
    recent = post({"type": "userFills", "user": addr})
    oldest = post({"type": "userFillsByTime", "user": addr, "startTime": 0})
    hist = post({"type": "historicalOrders", "user": addr})
    opn = {o["oid"] for o in post({"type": "openOrders", "user": addr})}
    fills = {}  # oid -> first fill time
    for x in recent + oldest:
        fills[x["oid"]] = min(fills.get(x["oid"], x["time"]), x["time"])
    hist_oids = {o["order"]["oid"] for o in hist}
    for oid, t in sample(sorted(fills.items(), key=lambda kv: kv[1])):
        r = post({"type": "orderStatus", "user": addr, "oid": oid})
        yield oid, age_days(t), oid in hist_oids, oid in opn, r["status"]
    for o in [o for o in hist if o["order"]["cloid"]][:4]:
        by_oid = post({"type": "orderStatus", "user": addr, "oid": o["order"]["oid"]})
        by_cl = post({"type": "orderStatus", "user": addr, "oid": o["order"]["cloid"]})
        yield o["order"]["oid"], by_oid["status"], by_cl["status"]
```

Sample: 33 addresses read, 28 with fills (the HLP vault and 4 others had none). 556 distinct
oids asked. 11 rows excluded as system fills with no user order (`dir` of
`Spot Dust Conversion` or `Liquidated Isolated Long`, all `unknownOid`). 545 user-order probes
remain, fill age 0 to 980 days.

## Results by fill age

| Fill age | Probed | Answered with a record | Answered `unknownOid` |
| --- | --- | --- | --- |
| under 1 day | 66 | 21 | 45 |
| 1 to 7 days | 4 | 4 | 0 |
| 7 to 30 days | 13 | 13 | 0 |
| 30 to 90 days | 56 | 53 | 3 |
| 90 to 180 days | 138 | 75 | 63 |
| 180 to 365 days | 112 | 76 | 36 |
| over 365 days | 152 | 111 | 41 |
| **total** | **545** | **357** | **188** |

Age does not predict the answer. Order count does:

| Observation | Value |
| --- | --- |
| Accounts with at least one `unknownOid` on a retained fill | 15 of 28 |
| Oldest order still answered with a record | 980.42 days, `0xfff81c5b5882769f2a4b8a6537b7251c9bc00005` oid `6400203246` |
| Youngest `unknownOid` on a retained fill | 0.005 days (7 minutes), `0x399965e15d4e61ec3529cc98b7f7ebb93b733336` oid `543846303874` |
| Oldest `unknownOid` on a retained fill | 830.54 days, `0xffc1994a9028830725745b42521eaf41fb901a33` oid `25008170261` |
| Orders present in `historicalOrders` or `openOrders` that answered `unknownOid` | 0 of 545 |
| Orders absent from both that still answered with a record | 57 (all at the window edge) |
| `unknownOid` rows placed before the oldest `historicalOrders` row | 184 of 188 |
| `unknownOid` rows placed after it | 4, each with exactly one older `historicalOrders` row |

Reading of the table:

- Every account with zero `unknownOid` has `historicalOrders` reaching its oldest fill. Those
  accounts never placed more than about 2000 orders.
- Every account with `unknownOid` has fills older than its `historicalOrders` window.
- An account idle for 145 days (`0x85ecf584…`) still answers for its last 2000 orders. A market
  maker (`0x399965e1…`) loses the record of a filled order within 7 minutes.
- The record window is slightly larger than the 2000 rows `historicalOrders` shows. 57 orders
  just past that page still answered.

Fills also outlast the documented bound. For `0xbc1bc64b…` a `userFillsByTime` walk from
`startTime: 0` paged 12,000 fills between 180 and 149 days old, and the account fills about
1750 times per day. So the "10000 most recent fills" statement is not what the API enforced on
the research date. **(observed on one account, not a documented fact)**

## Cloid versus oid

| Test | Result |
| --- | --- |
| 40 `historicalOrders` rows with a cloid, asked by oid and by cloid | 40 of 40 answered with a record by both keys, same `oid` in the body |
| 11 orders that answered `unknownOid` by oid, asked by the cloid on their fill row | 11 of 11 `unknownOid` by cloid too (accounts `0x85ecf584…` and `0x399965e1…`, ages 7 minutes to 146 days) |
| A live order's cloid asked with a different `user` (the HLP vault address) | `unknownOid`. Same for its oid. The lookup is scoped to the user. |

Retention does not differ between the two keys. Both go through the same `oid` request field
(`info.py:L613-617`) and hit the same record.

## Side findings

- **Fill rows can carry `cloid`.** 7 of 28 accounts had `cloid` on their recent `userFills`
  rows, matching the `historicalOrders` cloid for the same oid in 2000 of 2000 checked. On the
  account paged above, the key appears on fills newer than about 150 days and is absent on
  older fills. The docs example row and the 0.24.0 docstrings do not list it. The adapter comment
  at `exchange.py:523` ("they have no cloid on the wire") is true for the docs and for old fills,
  but not for recent fills of a cloid-placing account. **(observed, not documented)**
- `historicalOrders` rows carry `cloid` (null when the order had none). Observed on every
  account with rows.
- `userFills` can hold rows with no user order: `Spot Dust Conversion` (zero hash, `tid` 0)
  and liquidation rows. Both answer `unknownOid` for their oid. A fill-history cross-check must
  expect those.
- `historicalOrders` returned fewer than 2000 rows on several accounts that still lost records
  (for example 1447 rows on `0xdf9ea6ec…`). So the page does not expose the whole window.

## Open questions

1. The exact size and unit of the record window. The probe brackets it at "about 2000 orders,
   a little more than the `historicalOrders` page". Whether modifications, child TP/SL orders,
   or rejected orders count against it is unknown.
2. Whether `historicalOrders` and `orderStatus` read the same store, or two stores of similar
   size. The 57 edge rows suggest the `orderStatus` store is the larger one.
3. The true fill retention bound. The docs say 10,000. One account paged past 12,000. Not
   measured to the end.
4. The date the venue started stamping `cloid` on fill rows. One account puts it near 150 days
   before the research date. Not confirmed elsewhere.
5. Testnet was not probed. No testnet address is documented in the repo.
