# Hyperliquid: reduce-only orders, batch cancels, and market order limits

**Research date:** 2026-09-28
**Pinned SDK:** `hyperliquid-python-sdk==0.24.0` (`pyproject.toml` `~=0.24`, `uv.lock` resolves
`0.24.0`). SDK line numbers were checked against the installed 0.24.0 wheel in the project `.venv`.
**Feeds:** issue [#409](https://github.com/MarcosACH/tickwright/issues/409), parent map
[#408](https://github.com/MarcosACH/tickwright/issues/408).

> **Status: point-in-time capture, superseded in part. The ADRs are canonical.**
> Kept verbatim as evidence and not maintained. Where it disagrees with an ADR, the ADR wins. The
> testnet probes settled its leads:
> [`hyperliquid-reduce-only-cancel-market-probes.md`](hyperliquid-reduce-only-cancel-market-probes.md),
> [`hyperliquid-min-notional-reduce-only-probes.md`](hyperliquid-min-notional-reduce-only-probes.md),
> and [`hyperliquid-rate-limit-window-probe.md`](hyperliquid-rate-limit-window-probe.md).
>
> | In this note | Current answer |
> | --- | --- |
> | Q1: clamp or reject is unknown | **Confirmed: shrunk** to the position (P1). Paper copies it in [ADR-0057](../adr/0057-paper-reduce-only-orders.md). |
> | Q2: resize on a partial shrink is a lead | **Confirmed** (P2). The size shrinks with the position, then the order ends `reduceOnlyCanceled`. [ADR-0057](../adr/0057-paper-reduce-only-orders.md). |
> | Q3: against a smaller opposite position, unknown | **Confirmed: shrunk**, never flips the side (P3). [ADR-0057](../adr/0057-paper-reduce-only-orders.md). |
> | Q4: no max batch size documented | **Measured: 200 cancels per action** (P4). See [ADR-0055](../adr/0055-strategy-cancel-all.md). |
> | Section 7: when limited, 1 request every 10 seconds | **Confirmed.** A refused request does not restart the window and uses no budget. [ADR-0056](../adr/0056-flatten-retries-stops-and-resumes.md). |
> | Section 8 lead: is a dust close under $10 exempt? | **Refined.** A whole-position close is exempt. A partial close under $10 is rejected, reduce-only or not (P7, P9). [ADR-0058](../adr/0058-pre-trade-limits-for-reduce-only-and-flatten-orders.md). |
> | "None of these were run" | All probes ran on testnet on 2026-10-06 and 2026-10-07. |

Scope: facts only. This note does not choose a design. Nothing was sent to testnet or mainnet.

Citation convention:

- Docs: a page under `https://hyperliquid.gitbook.io/hyperliquid-docs`. Short names are listed in
  [Sources](#sources).
- SDK: `sdk:<path>:L<n>`, meaning `hyperliquid-python-sdk@0.24.0 hyperliquid/<path>`.
- Repo: `src/...:<line>` at `main` `03675ad`.
- A claim with no citation is marked **(lead)**. A lead needs a testnet probe. The probes are in
  [Probes that would settle the leads](#probes-that-would-settle-the-leads).

## Short answers

| # | Question | Answer | Confidence |
| --- | --- | --- | --- |
| 1 | Reduce-only order larger than the position | Docs do not say. The only documented error is `Reduce only order would increase position.` Clamp or reject is unknown. | Lead |
| 2 | Resting reduce-only order when the position shrinks or closes | A reduce-only order that "does not reduce position" is cancelled with status `reduceOnlyCanceled`. Resize on a partial shrink is not documented. | Close: documented. Shrink: lead |
| 3 | Reduce-only order that would flip the side | With no position, it is rejected with `Reduce only order would increase position.` With a smaller opposite position, the outcome is the same unknown as question 1. | Partly documented |
| 4 | Batch cancel | `cancel` (by oid) and `cancelByCloid`. Both take a list. No max batch size is documented. The response has one status per cancel, `"success"` or `{"error": ...}`. | High, except the max size |
| 5 | Cancel orders the engine did not place | The cancel action takes only asset and oid. List them with `openOrders` or `frontendOpenOrders`. The SDK's own example does exactly this. | High for the schema. Origin blindness is a lead |
| 6 | Market orders | The SDK prices an IOC limit at mid × (1 ± 5%). `market_close` sets `reduce_only=True`. On a thin book the unfilled rest is cancelled. | High |
| 7 | Rate limits | IP: 1200 weight per minute, a batch of n costs `1 + floor(n / 40)`. Address: a batch of n counts as n. Cancels get extra headroom. | High |
| 8 | Other | Cancels run before IOC orders in the same block. Reduce-only orders are rejected at 1000+ open orders. `scheduleCancel` exists. | High |

## What the repo does today

| Fact | Source |
| --- | --- |
| Every order goes out with `"r": False`. Reduce-only is deferred by ADR-0030. | `src/tickwright/venues/hyperliquid/exchange.py:630` |
| MARKET becomes an IOC limit at `latest × (1 ± slippage_bound)`, rounded passively. | `src/tickwright/venues/hyperliquid/exchange.py:640-652`, `:654-656` |
| `latest` is the last trade tick price, not the mid. | `src/tickwright/venues/hyperliquid/exchange.py:218`, `:644` |
| `slippage_bound` defaults to `0.05`, copied from the SDK default. | `src/tickwright/venues/hyperliquid/config.py:41-42` |
| Cancel uses `cancel` by oid when the oid is known, else `cancelByCloid`. One order per action. | `src/tickwright/venues/hyperliquid/exchange.py:345-352` |
| The cancel reader unpacks exactly one status. Any `{"error": ...}` becomes `ALREADY_GONE`. | `src/tickwright/venues/hyperliquid/exchange.py:888-892` |
| The placement reader also unpacks exactly one status. | `src/tickwright/venues/hyperliquid/exchange.py:860` |
| Reads use `account_address`, falling back to the wallet address. | `src/tickwright/venues/hyperliquid/exchange.py:113` |
| The adapter never sends the `f` (fast) flag or `vaultAddress`. | `src/tickwright/venues/hyperliquid/exchange.py:349`, `:352`, `:672` |

## 1. Reduce-only order larger than the position

| Fact | Source |
| --- | --- |
| The wire field is `r`, "reduceOnly", a boolean on every order. | [exchange-endpoint](#sources), "Place an order" |
| The UI definition: "An order that reduces a current position as opposed to opening a new position in the opposite direction". | [order-types](#sources), "Order options" |
| The one reduce-only placement error is `ReduceOnly`: `Reduce only order would increase position.` | [error-responses](#sources) |
| The historical status for it is `reduceOnlyRejected`, "Rejected due to reduce only". | [info-endpoint](#sources), "Query order status" |
| The SDK does not clamp. `market_close(coin, sz)` sends the caller's `sz` as given. | `sdk:exchange.py:L283-298` |

Not documented: whether an order bigger than the position is shrunk to the position, filled up
to the position and then cancelled, or rejected whole. **(lead)** Probe P1 settles it.

## 2. Resting reduce-only order when the position shrinks or closes

| Fact | Source |
| --- | --- |
| Status `reduceOnlyCanceled`: "Canceled reduced-only order that does not reduce position". | [info-endpoint](#sources), "Query order status" |
| Fixed-size TP/SL orders "will not resize with the position after being placed". This is stated for TP/SL only. | [tp-sl](#sources), "TP/SL associated with a position" |
| A WebSocket `nonUserCancel` event carries `coin` and `oid` for cancels the user did not send. | [ws-subscriptions](#sources), `WsNonUserCancel` |
| The adapter already maps any status ending in `anceled` or `ancel` to CANCELLED. | `src/tickwright/venues/hyperliquid/exchange.py:754-755` |

So a resting reduce-only order is cancelled by the venue once it no longer reduces. The obvious
case is a position that closes to zero.

Not documented:

- What happens on a partial shrink. The order may be resized, cancelled, or left at full size.
  **(lead)**
- When the check runs. It could be at the position change or only at match time. **(lead)**
- Whether `reduceOnlyCanceled` arrives through `nonUserCancel`. **(lead)**

Probe P2 settles all three.

## 3. Reduce-only order that would flip the side

| Fact | Source |
| --- | --- |
| An order that "would increase position" is rejected with `Reduce only order would increase position.` | [error-responses](#sources) |
| With no position, any order increases the position. So a reduce-only order on a flat coin is rejected. | Follows from the row above |
| `PositionFlipAtOpenInterestCap` exists. It is a separate open interest cap rule, not a reduce-only rule. | [error-responses](#sources) |

Not documented: a long 0.01 with a reduce-only sell of 0.03. The venue may fill 0.01 and drop the
rest, or reject the whole order. **(lead)** This is the same unknown as question 1. Probe P3
settles it.

## 4. Batch cancel

Actions:

| Action | Wire | SDK method | Source |
| --- | --- | --- | --- |
| Cancel by oid | `{"type": "cancel", "cancels": [{"a": asset, "o": oid}], "f"?: bool}` | `cancel`, `bulk_cancel` | [exchange-endpoint](#sources), `sdk:exchange.py:L300-331` |
| Cancel by cloid | `{"type": "cancelByCloid", "cancels": [{"asset": asset, "cloid": hex}], "f"?: bool}` | `cancel_by_cloid`, `bulk_cancel_by_cloid` | [exchange-endpoint](#sources), `sdk:exchange.py:L303-359` |
| Cancel all at a future time | `{"type": "scheduleCancel", "time"?: ms}` | `schedule_cancel` | [exchange-endpoint](#sources), `sdk:exchange.py:L361-387` |

Facts:

| Fact | Source |
| --- | --- |
| There is no immediate "cancel all" action. You list open orders and cancel them in a batch. | [exchange-endpoint](#sources) has no such action. `sdk:exchange.py` has no such method |
| Each cancel entry carries its own asset. So one action can cancel across coins. | [exchange-endpoint](#sources), cancel schema |
| No max batch size is documented. The rate limit page gives a batch of 79 as an example. | [rate-limits](#sources) |
| "Order and cancel errors are usually returned as a vector with same length as the batched request." | [error-responses](#sources) |
| A cancel status is `"success"` or `{"error": "Order was never placed, already canceled, or filled."}`. | [exchange-endpoint](#sources), "Cancel order(s)" |
| Some errors are found in pre-validation. Then "only one error is returned for the entire payload". | [error-responses](#sources) |
| The docs show no example body for `cancelByCloid`. Both tabs are empty. | [exchange-endpoint](#sources), "Cancel order(s) by cloid" |
| `f: true` (fast) is rejected for trigger orders. `f` must be omitted when false, or the action is rejected. | [exchange-endpoint](#sources) |
| The docs recommend the fast flag for latency. "Fast cancels cannot be used to cancel trigger orders." | [optimizing-latency](#sources) |
| SDK 0.24.0 never sends `f`. | `sdk:exchange.py:L308-317`, `L336-345` |
| `batchModify` exists and can change many orders in one action. | [exchange-endpoint](#sources), `sdk:exchange.py:L215-243` |

Impact on the repo: the cancel reader unpacks exactly one status
(`src/tickwright/venues/hyperliquid/exchange.py:888`). A batch cancel needs a per-index reader. It
also needs to handle the single pre-validation error for a whole batch.

Leads:

- A real maximum batch size, or a request body size cap. **(lead)**
- The `cancelByCloid` response shape. It is assumed to match `cancel`, which is what the adapter
  already parses. **(lead)**

## 5. Cancelling orders the engine did not place

| Fact | Source |
| --- | --- |
| Cancel by oid needs only `a` (asset) and `o` (oid). No cloid is needed. | [exchange-endpoint](#sources), "Cancel order(s)" |
| `openOrders` returns `coin`, `limitPx`, `oid`, `side`, `sz`, `timestamp`. The example has no `cloid`. | [info-endpoint](#sources), "Retrieve a user's open orders" |
| `frontendOpenOrders` adds `reduceOnly`, `isTrigger`, `orderType`, `origSz`, `tif`, `triggerPx`, `children`. | [info-endpoint](#sources), `sdk:info.py:L154-185` |
| A recorded `frontendOpenOrders` answer lists untriggered TP/SL orders (`"isTrigger": true`). It has no `cloid` field. | SDK test cassette `tests/cassettes/info_test/test_get_frontend_open_orders.yaml:L21-29` at tag `0.24.0` |
| Both reads take a `dex`. The default is the first perp dex. Other perp dexes need their own call. | [info-endpoint](#sources) |
| You must query the account's own address. An agent wallet address "leads to an empty result". | [info-endpoint](#sources), "User address". Also [nonces](#sources) |
| The SDK example `cancel_open_orders.py` lists `info.open_orders(address)` and cancels each by oid. | SDK `examples/cancel_open_orders.py:L9-12` at tag `0.24.0` |
| Weight: `openOrders` and `frontendOpenOrders` cost 20. `clearinghouseState` costs 2. | [rate-limits](#sources) |

So yes, the API can cancel any open order on the account by oid. The docs never say an order's
origin (UI, API, other bot) matters. That the venue ignores origin is inferred from the schema.
**(lead)** Probe P5 confirms it.

Leads:

- Whether `openOrders` includes untriggered trigger orders. `frontendOpenOrders` does. **(lead)**
- Whether either read ever carries `cloid` today. The examples do not. **(lead)**

## 6. Market orders

| Fact | Source |
| --- | --- |
| The API has no market order type. `t` is `limit` (`Alo`, `Ioc`, `Gtc`) or `trigger`. | [exchange-endpoint](#sources), "Place an order" |
| "IOC (immediate or cancel) will have the unfilled part canceled instead of resting." | [exchange-endpoint](#sources) |
| `DEFAULT_SLIPPAGE = 0.05`. | `sdk:exchange.py:L80-81` |
| Slippage price: mid from `allMids` unless `px` is given, times `1 + slippage` for a buy or `1 - slippage` for a sell. | `sdk:exchange.py:L112-130` |
| Rounding: 5 significant figures, then `6 - szDecimals` decimals for perps. It rounds to nearest, not passively. | `sdk:exchange.py:L131-132` |
| `market_open` sends an IOC limit with `reduce_only=False`. | `sdk:exchange.py:L245-260` |
| `market_close` reads `user_state` for the account (or vault) address. It finds the coin, defaults `sz` to `abs(szi)`, picks the opposite side, and sends an IOC limit with `reduce_only=True`. | `sdk:exchange.py:L262-298` |
| `market_close` closes one coin per call. With no position on the coin it returns `None`. | `sdk:exchange.py:L278-298` |
| `market_close` treats `sz=0` as "whole position", because it tests `if not sz`. | `sdk:exchange.py:L283-284` |
| Thin book, nothing matches: `IocCancel`, `Order could not immediately match against any resting orders.` Status `iocCancelRejected`. | [error-responses](#sources), [info-endpoint](#sources) |
| `MarketOrderNoLiquidity`: `No liquidity available for market order.` Status `marketOrderNoLiquidityRejected`. | [error-responses](#sources), [info-endpoint](#sources) |
| `Oracle`: `Order price too far from oracle`. Pre-validation also rejects an "order too far from reference price". | [error-responses](#sources) |
| A filled status carries `totalSz`, `avgPx`, and `oid`. | [exchange-endpoint](#sources), filled example |
| Maximum market order value depends on max leverage: $30M at 25x or more, $5M at 20x to 25x, $2M at 10x to 20x, else $500k. Limit orders get 10 times that. | [contract-specs](#sources) |
| UI TP/SL market orders use 10% slippage. TWAP suborders use 3%. | [tp-sl](#sources), [order-types](#sources) |

The adapter differs from the SDK in two ways. It prices off the last trade, not the mid. It also
rounds passively, so the bound is never exceeded (`src/tickwright/venues/hyperliquid/exchange.py:640-652`).

Leads:

- Whether a partial IOC fill answers `filled` with a smaller `totalSz`, or `resting`, or something
  else. **(lead)** The adapter reads fills from venue records anyway (ADR-0011).
- Which error an aggressive IOC limit gets on an empty book: `IocCancel` or
  `MarketOrderNoLiquidity`. **(lead)**
- The "too far from oracle" band. It is not documented. **(lead)**
- Whether the max market order value applies to an API IOC limit, or only the limit order cap
  applies. **(lead)**

## 7. Rate limits for closing and cancelling many at once

| Fact | Source |
| --- | --- |
| IP limit: REST shares 1200 weight per minute. | [rate-limits](#sources) |
| Every exchange action weighs `1 + floor(batch_length / 40)`. A batch of 79 weighs 2. | [rate-limits](#sources) |
| `clearinghouseState`, `orderStatus`, `allMids`, `l2Book` weigh 2. Other documented info reads weigh 20. | [rate-limits](#sources) |
| "A batched request with n orders (or cancels) is treated as one request for IP based rate limiting, but as n requests for address-based rate limiting." | [rate-limits](#sources) |
| Address limit: 1 request per 1 USDC traded since inception, plus a 10000 starting buffer. When limited, 1 request every 10 seconds. Actions only, not info reads. | [rate-limits](#sources) |
| Cancels get `min(limit + 100000, limit * 2)`, so open orders can still be cancelled when rate limited. | [rate-limits](#sources) |
| Sub-accounts count as separate users. | [rate-limits](#sources) |
| During congestion, an address may use 2x its previous day maker share of block space. The docs advise not to resend cancels that already have a result. | [rate-limits](#sources) |
| An action cancelled for a stale `expiresAfter` costs 5x the address limit. | [exchange-endpoint](#sources), "Expires After" |
| `userRateLimit` returns `cumVlm`, `nRequestsUsed`, `nRequestsCap`, `nRequestsSurplus`. | [info-endpoint](#sources), `sdk:info.py:L727-739` |
| WebSocket: at most 2000 messages per minute and 100 inflight posts across all connections. | [rate-limits](#sources) |

What it means for a flatten: closing N coins in one order action costs 1 to 2 IP weight but N
address requests. Cancelling M orders in one action costs M address requests, from the larger
cancel budget.

## 8. Other facts a safe flatten needs

| Fact | Source |
| --- | --- |
| Within a block, cancels run before actions that send GTC or IOC orders. | [hypercore-order-book](#sources) |
| "Cancels and ALO orders sent at time t will almost always execute before IOC and GTC orders sent at time t." | [optimizing-latency](#sources) |
| The open order limit is 1000, plus 1 per 5M USDC volume, capped at 5000. | [rate-limits](#sources) |
| With 1000 or more other open orders, a new reduce-only or trigger order is rejected. | [rate-limits](#sources) |
| Minimum order value is $10 (`Order must have minimum value of $10.`). | [error-responses](#sources) |
| Liquidation first sends market orders for the full position. Its cancels show as `liquidatedCanceled`. | [liquidations](#sources), [info-endpoint](#sources) |
| `scheduleCancel` cancels all open orders at a time at least 5 seconds ahead. At most 10 triggers per day, reset at 00:00 UTC. Status `scheduledCancel`. Noted only. A dead man's switch is out of scope. | [exchange-endpoint](#sources), `sdk:exchange.py:L361-387` |
| The adapter already maps `scheduledCancel` to CANCELLED. | `src/tickwright/venues/hyperliquid/exchange.py:747`, `:754` |
| The 100 highest nonces are kept per signer. A new nonce must be above the smallest and never reused. | [nonces](#sources) |
| The docs suggest batching orders and cancels every 0.1 seconds, with IOC and GTC apart from ALO. | [nonces](#sources) |
| Positions and open orders are per perp dex (`dex` field). The SDK `user_state` also takes `dex`. | [info-endpoint](#sources), `sdk:info.py:L86`, `L133` |

Leads:

- Whether a reduce-only close of a dust position under $10 is exempt from the minimum.
  **(lead)** A flatten could leave dust if it is not exempt. Probe P7.

## Probes that would settle the leads

None of these were run. Each runs on testnet with a funded test account and a liquid coin such as
ETH. Record the raw `/exchange` and `/info` bodies.

| Probe | Steps | Settles |
| --- | --- | --- |
| P1 | Open long 0.02. Send a reduce-only IOC sell of 0.05 at an aggressive price. Record the status and `totalSz`. Repeat with a reduce-only GTC sell of 0.05 above the market, then read `frontendOpenOrders` for its `sz`. | Q1 |
| P2 | Open long 0.02. Rest a reduce-only GTC sell of 0.02 above market. Sell 0.01 without reduce-only. Read the resting order's `sz`. Then close the rest. Read `orderStatus` for the resting oid. Watch `userEvents` for `nonUserCancel`. | Q2 |
| P3 | Flat account: send a reduce-only IOC sell. Then open long 0.01 and send a reduce-only IOC sell of 0.03. Record both statuses and the final position. | Q3 |
| P4 | Batch cancel three oids in one action: one live, one filled, one made up. Repeat with `cancelByCloid`. Record the `statuses` array. Try a batch of 200 to look for a size cap. | Q4 |
| P5 | Place an order by hand in the testnet UI. Read `openOrders` and `frontendOpenOrders`. Look for `cloid` and trigger orders. Cancel it by oid from the API. | Q5 |
| P6 | On a thin testnet coin, send an IOC larger than the visible book at a 5% bound. Then send one on an empty side. Record statuses and error strings. | Q6 |
| P7 | Leave a position worth under $10. Close it with a reduce-only IOC. Record the status. | Q8 |
| P8 | Read `userRateLimit`, batch-cancel n orders, read it again. Check that `nRequestsUsed` grows by n. | Q7 |

## Sources

| Short name | URL |
| --- | --- |
| exchange-endpoint | https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint |
| info-endpoint | https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint |
| error-responses | https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/error-responses |
| rate-limits | https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits |
| nonces | https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/nonces-and-api-wallets |
| optimizing-latency | https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/optimizing-latency |
| ws-subscriptions | https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions |
| order-types | https://hyperliquid.gitbook.io/hyperliquid-docs/trading/order-types |
| tp-sl | https://hyperliquid.gitbook.io/hyperliquid-docs/trading/take-profit-and-stop-loss-orders-tp-sl |
| contract-specs | https://hyperliquid.gitbook.io/hyperliquid-docs/trading/contract-specifications |
| liquidations | https://hyperliquid.gitbook.io/hyperliquid-docs/trading/liquidations |
| hypercore-order-book | https://hyperliquid.gitbook.io/hyperliquid-docs/hypercore/order-book |
| SDK source | https://github.com/hyperliquid-dex/hyperliquid-python-sdk/tree/0.24.0 |

Docs pages were read on 2026-09-28 through their `.md` form. The docs are not versioned, so a
later read may differ.
