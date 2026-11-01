# Hyperliquid "Price too far from oracle" rule

Captured 2026-10-31, with one probe run on 2026-11-01. SDK: `hyperliquid-python-sdk` 0.24.0.

This note collects what the venue says about the `oracleRejected` status. It feeds issue #600. It is
a reference, not a decision.

## How the sources were read

- Docs: the Markdown copy of each GitBook page (`<page>.md`) and the full dump at
  `https://hyperliquid.gitbook.io/hyperliquid-docs/llms-full.txt`. I searched the full dump for
  "too far", "reference price", "from oracle", "than oracle", "price band", and percent values.
- SDK: the project `.venv` reports `hyperliquid-python-sdk` 0.24.0. The `.venv` files are blocked by
  the local permission settings. So the SDK source below is the `0.24.0` git tag (commit
  `2fdb18f9`). `sdk:` below means
  `https://github.com/hyperliquid-dex/hyperliquid-python-sdk/blob/0.24.0/hyperliquid/`.
- API: read-only `POST /info` calls on mainnet and testnet. These were not signed. Only the probe
  placed orders, on testnet. See "Probe, 2026-10-31" and "Aggressive probe, 2026-11-01".

## 1. What triggers it

| Fact | Source |
| --- | --- |
| Error `Oracle` has the text `Order price too far from oracle`. Its source is `Order`. | [error-responses](#sources) |
| Order status `oracleRejected` means "Rejected due to price too far from oracle". | [info-endpoint](#sources), order status table |
| The reference price named is the oracle price. Not the mark, not the mid. | Both rows above |
| The oracle is "the weighted median of each validator's submitted oracle prices", weighted by stake. | [oracle](#sources) |
| Each validator's price is a weighted median of CEX spot mids (Binance, OKX, Bybit, Kraken, Kucoin, Gate IO, MEXC, Hyperliquid spot). | [oracle](#sources) |
| For BTC, Hyperliquid spot is left out of the oracle. | [oracle](#sources) |
| The oracle "does not depend on hyperliquid's market data at all". It updates about every 3 seconds. | [robust-price-indices](#sources) |

Mark price is a different number. It is the median of three inputs, and two of them come from the
Hyperliquid book. See
[hyperliquid-margin-leverage-liquidation-mark.md](hyperliquid-margin-leverage-liquidation-mark.md),
section "4.2 Mark price".

## 2. The band

- The size of the band is **not documented**. No page gives a percent or a formula.
- Whether it is symmetric is **not documented**.
- Whether it depends on side is **not documented**.
- Whether it differs per asset is **not documented**.
- The SDK has no oracle check. A search of the `0.24.0` tag for "oracle" finds only the HIP-3
  deployer helpers (`sdk:exchange.py:899-956`).

## 3. Which orders it applies to

- The docs do not say which order types the `Oracle` check covers. **Not documented.**
- The docs do not mention a reduce-only exemption. **Not documented.**
- The error table lists `Oracle` as a per-order error, not a pre-validation error.
  [error-responses](#sources)
- Order types that exist: limit with `tif` `Alo`, `Ioc`, or `Gtc`, or `trigger`. `r` is the
  reduce-only flag. [exchange-endpoint](#sources), "Place an order"

Observed in issue #600 (first-party `orderStatus` responses, quoted from the issue):

| Order | `limitPx` | Status |
| --- | --- | --- |
| reduce-only buy, IOC | 83256.0 | `oracleRejected` |
| reduce-only buy, IOC | 83310.0 | `oracleRejected` |
| plain buy, GTC, at the touch | 79244.0 | `oracleRejected` |

So, on testnet at least, the check hit IOC and GTC orders. It hit reduce-only orders too. A
reduce-only flag did not exempt the order.

## 4. Testnet vs mainnet

| Fact | Source |
| --- | --- |
| The API is the same on both. Only the base URL changes. | [api](#sources) |
| No page says the oracle rule differs on testnet. **Not documented.** | Full-text search |
| No page says how the testnet oracle is produced. **Not documented.** | Full-text search |
| The only documented testnet difference near this topic is the liquidation size (10k USDC, not 100k). | [liquidations](#sources) |

### API snapshot, 2026-10-31

Requests, sent to `https://api.hyperliquid.xyz/info` and `https://api.hyperliquid-testnet.xyz/info`:

```
POST /info  {"type":"metaAndAssetCtxs"}
POST /info  {"type":"l2Book","coin":"BTC"}
```

BTC is asset 0 on mainnet and asset 3 on testnet. Six samples, about 10 seconds apart:

| UTC | Main oracle | Test oracle | Main mark | Test mark | Test mid | Test mark vs oracle |
| --- | --- | --- | --- | --- | --- | --- |
| 19:55:03 | 77548.1 | 77548.1 | 77551.7 | 76996.0 | 77108.5 | -0.71% |
| 19:55:11 | 77548.1 | 77548.0 | 77551.7 | 77027.0 | 77010.5 | -0.67% |
| 19:55:21 | 77533.4 | 77532.9 | 77537.0 | 76922.8 | 76850.5 | -0.79% |
| 19:55:31 | 77526.1 | 77526.0 | 77529.6 | 76914.6 | 76844.0 | -0.79% |
| 19:55:41 | 77527.0 | 77526.1 | 77530.0 | 76913.2 | 76844.0 | -0.79% |
| 19:55:51 | 77527.0 | 77527.0 | 77530.0 | 76912.4 | 76844.5 | -0.79% |

What the snapshot shows:

- The testnet BTC oracle matched the mainnet BTC oracle within 0.9 USD in every sample.
- The mainnet mark sat within 0.005% of its oracle.
- The testnet mark sat about 0.8% below its oracle. It followed the testnet book, not the oracle.

This is one short sample. It does not prove that the testnet oracle is always a copy of mainnet.

`{"type":"perpsAtOpenInterestCap"}` at 19:57:09 UTC returned `[]` on testnet. On mainnet it listed
eight small assets. BTC was in neither list.

### Candles during the cycle 551 window

Request (both networks), 2026-10-30 16:40 to 17:50 UTC:

```
POST /info  {"type":"candleSnapshot","req":{"coin":"BTC","interval":"5m",
             "startTime":1793378400000,"endTime":1793382600000}}
```

| Window (UTC) | Mainnet close range | Testnet close range | Testnet above mainnet |
| --- | --- | --- | --- |
| 16:40 to 17:20 | 77178 to 77440 | 79343, flat | 2.5% to 2.8% |
| 17:25 to 17:50 | 77139 to 77312 | 79086 to 79198 | 2.4% to 2.6% |

The testnet book sat about 2.5% above mainnet for the whole window. Mainnet trades are not the
oracle. But today the mainnet mark is within 0.005% of its oracle.

## 5. Related rules that can reject a price

| Rule | Exact string | Status | Band documented? | Source |
| --- | --- | --- | --- | --- |
| Too far from oracle | `Order price too far from oracle` | `oracleRejected` | No | [error-responses](#sources), [info-endpoint](#sources) |
| Too far from reference price (pre-validation) | Not documented. The page says only "order too far from reference price". | Not documented | No | [error-responses](#sources) |
| More aggressive than oracle at open interest cap | `Order rejected due to price more aggressive than oracle while at open interest cap` | `tooAggressiveAtOpenInterestCapRejected` | See below | [error-responses](#sources), [info-endpoint](#sources) |
| Resting order canceled at open interest cap | Not documented | `openInterestCapCanceled` ("Canceled due to order being too aggressive when open interest was at cap") | No | [info-endpoint](#sources) |
| Post-only would cross | `Post only order would have immediately matched, bbo was {bbo}.` | `badAloPxRejected` | Tied to best bid and ask | [error-responses](#sources) |
| IOC finds no match | `Order could not immediately match against any resting orders.` | `iocCancelRejected` | Not a band | [error-responses](#sources) |
| No liquidity for market order | `No liquidity available for market order.` | `marketOrderNoLiquidityRejected` | Not a band | [error-responses](#sources) |
| Bad trigger price | `Invalid TP/SL price.` | `badTriggerPxRejected` | No | [error-responses](#sources) |

Notes on these rows:

- A pre-validation error returns one error for the whole batch. [error-responses](#sources)
- The reference price behind "too far from reference price" is not named. **Not documented.**
- An archived copy of the old "Risks" page (Wayback, 2026-08-03) said this: "When an asset hits the
  open interest cap, no new positions can be opened. Furthermore, orders cannot rest further than 1%
  from the oracle price. HLP is exempt from these rules." [risks-archive](#sources)
- That "Risks" page is now gone. The live URL returns "Page Not Found". So the 1% rule is a past
  primary statement. It is not confirmed as current.
- The 1% rule covers assets at the open interest cap only. Its error is `TooAggressiveAtOpenInterestCap`,
  not `Oracle`.
- No "80% away from reference price" rule appears anywhere in the current docs.

## The SDK market-order helper

| Fact | Source |
| --- | --- |
| `DEFAULT_SLIPPAGE = 0.05`. | `sdk:exchange.py:80-81` |
| The start price is `px` if given. Otherwise it is the mid from `all_mids`. | `sdk:exchange.py:119-123` |
| A buy uses `px * (1 + slippage)`. A sell uses `px * (1 - slippage)`. | `sdk:exchange.py:129-130` |
| It rounds to 5 significant figures, then `6 - szDecimals` decimals for perps. | `sdk:exchange.py:131-132` |
| `market_open` sends an IOC limit with `reduce_only=False`. | `sdk:exchange.py:245-260` |
| `market_close` sends an IOC limit with `reduce_only=True`. | `sdk:exchange.py:262-298` |
| `allMids` uses the last trade when the book is empty. | [info-endpoint](#sources), "Retrieve mids for all coins" |
| The SDK never reads `oraclePx` for a market order. | `sdk:exchange.py:112-132` |

Tickwright's adapter starts from the last trade, not the mid. It caps the price with
`mark * (1 ± slippage_bound)` and takes the tighter one
(`src/tickwright/venues/hyperliquid/exchange.py:882-902`). The default bound is 5%
(`src/tickwright/venues/hyperliquid/config.py:43`). More detail is in
[hyperliquid-reduce-only-cancel-market.md](hyperliquid-reduce-only-cancel-market.md), section
"6. Market orders".

## Leads, not verified

- A third-party guide says the band is "e.g., 5% for most assets, but check the docs". It also says
  the band "can vary" by asset. It gives no source. [onfinality](#sources) **(lead)**
- Another third-party guide says the band is measured from the mark price. It gives no number.
  [onekey](#sources) **(lead)**
- The SDK's own default puts a market order 5% from the mid. If the band were under 5% on mainnet,
  the SDK default would often fail. This suggests a wider band, but it proves nothing. **(inference)**
- The testnet mark can follow the testnet book. Two of the three mark inputs use the Hyperliquid
  mid or book. When the book drifts, the median picks a book value. The 2026-10-31 snapshot fits
  this. **(inference from the documented formula)**
- The 83256 and 83310 IOC buys were 7.6% to 7.9% above the mainnet price in that window. **(inference,
  if the testnet oracle tracked mainnet then too)**
- The GTC buy at 79244 was rejected at 2026-10-31 03:32:31 UTC. Source: the engine-a container log
  on the soak host, `order.rejected`, cloid `0x550f20f310df3afd11c90c8c3a0efc15`.
- In that minute, mainnet BTC traded from 77256 to 77304. Testnet BTC sat flat at 79244 from 03:30
  to 03:35. Source: `candleSnapshot`, 1m, both networks, fetched 2026-10-31.
- So the GTC buy was 2.5% to 2.6% above the mainnet price. If the testnet oracle tracked mainnet
  then, the band for that order was under 2.6%. **(inference, the oracle at that time is not
  recorded)**
- That fits badly with the SDK default, which prices market orders 5% from the mid. One possible
  reason: the check may look at the price the order would trade at, not its limit price. On
  testnet every resting order sat near 79244. **(hypothesis, not documented)** The aggressive probe
  supports it.

## Probe, 2026-10-31

Script: [hyperliquid-oracle-price-band.probe.py](hyperliquid-oracle-price-band.probe.py), `passive`
mode. It placed post-only BTC orders of about 12 USD on testnet, account `0x049d…5F76`. That is not a soak account.
Each order was canceled at once. No order filled, and none was left open.

Run from 2026-10-31 20:44:11 to 20:44:48 UTC. During the run, the testnet oracle was 77619 to 77629.
The mark and the book sat 0.5% to 0.7% below it.

| Distance from oracle | Sell above oracle | Buy below oracle |
| --- | --- | --- |
| 1% | resting | resting |
| 2% | resting | resting |
| 3% | resting | resting |
| 5% | resting | resting |
| 8% | resting | resting |

What the probe shows:

- A resting order on the passive side may sit at least 8% from the oracle. No rejection appeared.
- So a limit-price check at 2.6% does not explain the GTC buy at 79244. Not for passive orders
  at least.

What the probe cannot show:

- Every rejected order in issue #600 was a buy above the oracle. That is the aggressive side.
  The probe tested only the passive side.
- A post-only buy above the oracle needs the ask to be above it too. During the run the ask was
  below the oracle, so that order would have crossed.
- So the venue may check the aggressive side only, or check the fill price. The probe cannot tell
  these apart.

## Aggressive probe, 2026-11-01

Same script, `aggressive` mode, same account. It sent an IOC buy priced 5% above the oracle, then an
IOC sell priced 5% below it. Both crossed the book. Each was about 12 USD. The sell closed the buy.
The account ended flat, with no open order.

Run from 2026-11-01 00:17:18 to 00:17:21 UTC. The oracle was 77489 to 77492. The book sat 0.5% to
0.9% below it.

| Order | Limit vs oracle | Fill vs oracle | Venue status |
| --- | --- | --- | --- |
| IOC buy | +5.00% | -0.41% | filled |
| IOC sell | -5.00% | -0.74% | filled |

What the probe shows:

- The venue accepted a limit 5% past the oracle on the aggressive side. Both sides filled near the
  oracle.
- So the venue does not reject on the limit price alone at 5%. Our MARKET bound is 5%.
- The GTC buy at 79244 was priced at the touch. Its limit was also the price it would trade at. It
  was rejected at about 2.5% above the mainnet price.
- So the check most likely looks at the price the order would trade at. **(inference, the oracle
  at 03:32 is not recorded)**

What the probe cannot show:

- It ran on testnet. Mainnet is assumed to run the same rule. The docs do not say so.
- It does not give the band size for the trade price on the aggressive side.

## What this means for issue #600

The issue asks two questions.

1. **Is this a testnet effect?** Mostly yes. In the cycle 551 window the testnet book sat about 2.5%
   above mainnet for over an hour. The testnet mark followed the book. So the mark cap did not pull
   the price back toward the oracle. Our 5% bound then put the IOC buys 7.6% to 7.9% above the
   mainnet price. That assumes the testnet oracle tracked mainnet then, as it did on 2026-10-31.
2. **Can our bound fall outside the band on mainnet?** Not while the book sits near the oracle. The
   aggressive probe filled IOC orders priced 5% past the oracle, on both sides. In one sample the
   mainnet mark sat within 0.005% of its oracle.

What is left for mainnet:

- The rule was tested on testnet only. Mainnet is assumed to run the same rule.
- If the mainnet book itself moves far from the oracle, an order that trades there can be rejected.
  That is how testnet failed in issue #600. No bound on our side fixes it.

A different bound would not fix testnet. When the book sits 2.5% above the oracle, a buy that trades
there is rejected. A buy priced closer to the oracle does not reach the ask.

Still open:

- Whether mainnet runs the same rule as testnet.
- The band for the trade price on the aggressive side, and whether it depends on asset or order
  type. The GTC buy suggests under 2.6%. The passive side allows at least 8%.
- Whether reduce-only orders get any exemption. The issue #600 rejections suggest they do not.
- The oracle value for the GTC buy at 79244. No way to read a past oracle value was found.
- How the testnet oracle is produced. On 2026-10-31 it matched mainnet, but that is not documented.

## Sources

| Name | URL |
| --- | --- |
| error-responses | https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/error-responses |
| info-endpoint | https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint |
| exchange-endpoint | https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint |
| api | https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api |
| oracle | https://hyperliquid.gitbook.io/hyperliquid-docs/hypercore/oracle |
| robust-price-indices | https://hyperliquid.gitbook.io/hyperliquid-docs/trading/robust-price-indices |
| liquidations | https://hyperliquid.gitbook.io/hyperliquid-docs/trading/liquidations |
| full docs dump | https://hyperliquid.gitbook.io/hyperliquid-docs/llms-full.txt |
| risks-archive | https://web.archive.org/web/20260803060006/https://hyperliquid.gitbook.io/hyperliquid-docs/risks |
| SDK 0.24.0 | https://github.com/hyperliquid-dex/hyperliquid-python-sdk/tree/0.24.0 |
| onfinality (lead) | https://onfinality.io/en/learn/hyperliquid-api-error-handling-order-rejections |
| onekey (lead) | https://onekey.so/blog/ecosystem/hyperliquid-error-messages/ |
