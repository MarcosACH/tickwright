# Hyperliquid: two resting orders under one cloid

**Research date:** 2026-11-13 (UTC)
**Pinned SDK:** `hyperliquid-python-sdk==0.24.0`
**Feeds:** issue [#652](https://github.com/MarcosACH/tickwright/issues/652).

Scope: facts only. This note does not choose a fix.

## The question

The stray check from #642 reads a `FAILED` saga by its cloid. An earlier life of the account may
still have an order resting under the same cloid. Issue #652 asks three things:

1. Does the venue accept a place under a cloid whose earlier order still rests?
2. If it does, which order does `orderStatus` by that cloid return?
3. If it does, which order does a cancel by that cloid remove?

## Short answer

1. **Yes.** Both orders rest, each with its own oid.
2. **The newer order**, in 10 of 10 reads over two runs.
3. **The newer order.** The older one stays open.

The newer order won whether its price was nearer the book or farther from it. So price did not
decide it in these runs. Both runs fit "the newest order wins". No venue doc states that rule.

## Documented facts

- The venue doc does not say which order `orderStatus` by cloid returns when two share it
  (ADR-0011, #354 amendment).
- A third-party doc says a duplicate cloid is rejected:
  [Chainstack, cancel by cloid](https://docs.chainstack.com/reference/hyperliquid-exchange-cancel-order-by-cloid).
  The runs below contradict that while the first order still rests.

## Probe method

Script: [hyperliquid-duplicate-cloid.probe.py](hyperliquid-duplicate-cloid.probe.py). Testnet
account `0x049d…5F76`. That is not a soak account.

Each run used one fresh random cloid:

1. Place a post-only BTC buy of about 12 USD below the oracle.
2. About 3 seconds later, place a second one under the same cloid, at a different price.
3. Read `frontendOpenOrders`, then `orderStatus` by the cloid 5 times, 1 second apart.
4. Read `orderStatus` by each oid.
5. Cancel once by the cloid, then read `frontendOpenOrders` again.
6. Cancel any probe order still open, by oid.

No order filled. Both runs ended with no open order and a flat BTC position.

## Results

The testnet oracle was about 70100 to 70300 during both runs.

| | Run 1, 02:48 UTC | Run 2, 02:53 UTC |
| --- | --- | --- |
| Commit | `37948f99` | `638f92b3` |
| First order | oid 65200508053, px 66793 | oid 65200774149, px 65909 |
| Second order | oid 65200511023, px 66098 | oid 65200779214, px 66610 |
| Newer order is | farther from the book | nearer the book |
| Second place answer | `resting` | `resting` |
| `frontendOpenOrders` under the cloid | both | both |
| `orderStatus` by each oid | both `open` | both `open` |
| `orderStatus` by cloid, 5 reads | second, 5 of 5 | second, 5 of 5 |
| Cancel by cloid answer | `success` | `success` |
| Open after cancel by cloid | first only | first only |

Side finding: a cancel by oid for an order already canceled answers
`{"error": "Order was never placed, already canceled, or filled. asset=3"}`. The batch status is
still `ok`.

## What the runs cannot show

- Whether "newest wins" is a rule or an accident of two runs. The venue does not document it.
- Whether it holds on mainnet. Both runs were on testnet.
- Whether it holds when the two orders are far apart in time, as across two lives of an account.
  The runs placed them about 3 seconds apart.
- Whether it holds for orders on opposite sides or with other order types.
- Whether the older order can be read by cloid once the newer one is gone. The runs did not read
  by cloid after the cancel.
