# Hyperliquid testnet probes: reduce-only orders, batch cancels, and market orders

**Probe date:** 2026-10-06
**Venue:** Hyperliquid testnet (`https://api.hyperliquid-testnet.xyz`). Nothing was sent to mainnet.
**SDK:** `hyperliquid-python-sdk==0.24.0`, from the project `.venv`.
**Feeds:** ticket [#416](https://github.com/MarcosACH/tickwright/issues/416), parent map
[#408](https://github.com/MarcosACH/tickwright/issues/408).
**Settles the leads in:** [`hyperliquid-reduce-only-cancel-market.md`](hyperliquid-reduce-only-cancel-market.md),
section "Probes that would settle the leads". Probe ids P1 to P8 are from that table.

Scope: facts only. This note does not choose a design.

Setup: an agent (API) wallet acting for a master account with about $928 of test USDC. Coin ETH
(`szDecimals` 4, isolated 5x on this account) unless the probe says otherwise. Every probe started
and ended flat with no open orders. Bodies below are copied from the raw responses. Long bodies are
cut to the fields that matter.

## Short answers

| Probe | Question | Answer |
| --- | --- | --- |
| P1 | Reduce-only order larger than the position | **Shrunk to the position.** Not rejected. True for IOC and for a resting GTC. |
| P2 | Resting reduce-only order when the position shrinks, then closes | Its size shrinks with the position. When the position closes, it ends as `reduceOnlyCanceled`. |
| P3 | Reduce-only order with no position, or larger than an opposite position | No position: rejected. Smaller opposite position: shrunk to it. It never flips the side. |
| P4 | Batch cancel | One status per cancel. **At most 200 cancels per action.** At 201 the whole action is refused. |
| P5 | Orders placed by hand in the web UI | No cloid. Cancel by oid works. `openOrders` hides trigger fields. Cancelling a parent cancels its TP and SL. |
| P6 | Market order on a thin book | Nothing inside the bound: rejected. More size than the book holds at the limit: partial fill, rest cancelled. |
| P7 | Close a position worth under $10 | Works, with or without reduce-only. The $10 minimum applies to opening orders. |
| P8 | Rate limit cost of a batch cancel | Each cancel in the batch counts as one request, even a failed one. A refused action costs nothing. |

## P1. Reduce-only order larger than the position

IOC. Long 0.02 ETH. Reduce-only IOC sell of 0.05 at mid × 0.95:

```json
{"status": "ok", "response": {"type": "order", "data": {"statuses": [
  {"filled": {"totalSz": "0.02", "avgPx": "2686.6", "oid": 62014444172}}]}}}
```

The position after it was `null` (flat). No error, no short.

GTC. Long 0.02 ETH. Reduce-only GTC sell of 0.05 at mid × 1.05. The order rested. Then
`frontendOpenOrders`:

```json
{"coin": "ETH", "side": "A", "limitPx": "2821.1", "sz": "0.02", "oid": 62014449551,
 "reduceOnly": true, "orderType": "Limit", "origSz": "0.05", "tif": "Gtc", "cloid": null}
```

`origSz` keeps the requested size. `sz` is the size the venue will fill.

## P2. Resting reduce-only order when the position shrinks, then closes

Long 0.02. Rested a reduce-only GTC sell of 0.02 above the market. Sold 0.01 without
reduce-only. The resting order now read `"sz": "0.01", "origSz": "0.02"`. Sold the last 0.01
without reduce-only. The order left `frontendOpenOrders`. `orderStatus` for its oid:

```json
{"status": "order", "order": {"order": {"oid": 62014476975, "sz": "0.01", "origSz": "0.02",
 "reduceOnly": true}, "status": "reduceOnlyCanceled", "statusTimestamp": 1791318606747}}
```

Streams (a second run, P2b). The `orderUpdates` channel carried the cancel in the same message as
the closing fill:

```json
{"order": {"coin": "ETH", "side": "A", "limitPx": "2822.5", "sz": "0.01", "oid": 62014739273,
 "origSz": "0.01", "reduceOnly": true}, "status": "reduceOnlyCanceled",
 "statusTimestamp": 1791318879266}
```

The `userEvents` channel sent only the fill. It sent no `nonUserCancel` for this cancel.

## P3. Reduce-only order with no position, or against a smaller opposite position

Flat account. Reduce-only IOC sell 0.01, then reduce-only GTC sell 0.01 above the market. Both
answered:

```json
{"error": "Reduce only order would increase position. asset=4"}
```

The top-level `status` was still `"ok"`. The error lives in the per-order status. The string ends
with ` asset=<n>`, which the docs do not show.

Long 0.01. Reduce-only IOC sell 0.03: `{"filled": {"totalSz": "0.01", ...}}`. Final position flat.

## P4. Batch cancel

By oid. One batch with a live oid, a filled oid, and a made-up oid (`1`):

```json
{"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success",
  {"error": "Order was never placed, already canceled, or filled. asset=4"},
  {"error": "Order was never placed, already canceled, or filled. asset=4"}]}}}
```

By cloid. The same three cases gave the same three statuses. A filled order and a made-up id share
one error string, so the string cannot tell them apart.

Size cap. 200 live orders cancelled in one action: 200 statuses, all `"success"`. Then made-up
oids only:

| Batch size | Response |
| --- | --- |
| 200 | `"status": "ok"`, 200 statuses |
| 201 | `{"status": "err", "response": "Signed action over weight limit of 200."}` |
| 240, 400, 1000, 3000 | same error |

An action over the cap is refused whole. There are no per-order statuses.

## P5. Orders placed by hand in the web UI

The operator placed an ETH limit buy with a TP and an SL in the testnet UI, from the master wallet.

`openOrders` listed three orders. The TP and SL looked like plain reduce-only limit orders:

```json
{"coin": "ETH", "side": "A", "limitPx": "1980.0", "sz": "0.0507", "oid": 62015425745,
 "origSz": "0.0507", "reduceOnly": true}
```

`frontendOpenOrders` showed the trigger fields, and nested the TP and SL under the parent as
`children`:

```json
{"oid": 62015425745, "triggerCondition": "Price above 2200", "isTrigger": true,
 "triggerPx": "2200.0", "reduceOnly": true, "orderType": "Take Profit Market", "tif": null,
 "cloid": null}
```

None of the three had a cloid. Cancel by oid from the API worked on the parent. That one cancel
also cancelled the TP and SL. All three read `"status": "canceled"` with the same
`statusTimestamp`.

P5b repeated this from the API with `grouping="normalTpsl"`. The TP and SL placed as
`"waitingForFill"`. Two batches that cancel every oid from `openOrders`:

| Batch order | Statuses |
| --- | --- |
| As `openOrders` lists them (TP, SL, then parent) | `success`, `success`, `success` |
| Parent first | `success`, then `already canceled` error for both children |

A standalone trigger order (no parent) cancelled by oid: `success`. In `openOrders` it showed only
its limit price, not its trigger price.

## P6. Market order on a thin book

Coin WIF on testnet. Mid 0.2677. Best bid 0.2478, best ask 0.2876 with 136 WIF. Nothing rested
inside 5% of mid on either side.

SDK `market_open` buy 100 (limit at mid × 1.05):

```json
{"error": "Order could not immediately match against any resting orders. asset=78"}
```

IOC buy 200 at the best ask 0.2876, where the level held 136:

```json
{"filled": {"totalSz": "136.0", "avgPx": "0.2876", "oid": 62014694776}}
```

The other 64 were cancelled with no error. Then SDK `market_close` on the 136 long gave the same
`could not immediately match` error. The position stayed open. A reduce-only IOC at the third bid
level closed it.

A reduce-only IOC at the best bid also missed once. The bid had moved between the book read and the
order. This is a race, not a venue rule.

## P7. Closing a position worth under $10

Opening buy of 0.0037 ETH (about $9.9):

```json
{"error": "Order must have minimum value of $10. asset=4"}
```

A long of 0.0037 (position value $9.95) closed fine with a reduce-only IOC sell of 0.0037. In an
earlier run, a non-reduce-only IOC sell of 0.0037 also closed it. The limit price was mid × 0.95,
so the order's notional was about $9.45.

## P8. Rate limit cost of a batch cancel

`userRateLimit` before and after each action:

| Action | `nRequestsUsed` |
| --- | --- |
| before | 396 |
| cancel 200 live orders in one action | 596 |
| cancel 1000 made-up oids (refused) | 596 |
| cancel 200 made-up oids (all errors) | 796 |

`nRequestsCap` was 97197. This account had `cumVlm` 87197.94, so the cap matched 10000 plus
volume.
