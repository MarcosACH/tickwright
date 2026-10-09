# Hyperliquid testnet probes: the $10 minimum on orders that reduce a position

**Probe date:** 2026-10-07
**Venue:** Hyperliquid testnet (`https://api.hyperliquid-testnet.xyz`). Nothing was sent to mainnet.
**SDK:** `hyperliquid-python-sdk==0.24.0`, from the project `.venv`.
**Feeds:** ticket [#435](https://github.com/MarcosACH/tickwright/issues/435), parent map
[#408](https://github.com/MarcosACH/tickwright/issues/408).
**Follows:** P7 in [`hyperliquid-reduce-only-cancel-market-probes.md`](hyperliquid-reduce-only-cancel-market-probes.md).
P7 closed a whole position under $10 with an IOC order. It never tried a resting order, or an
order that closes only part of a position. This note does.

Scope: facts only. This note does not choose a design.

Setup: the same agent wallet as the earlier probes. Coin ETH (`szDecimals` 4). Each run started and
ended flat with no open orders. A run opened a long of 0.0051 ETH (about $13) with an IOC buy.
Resting orders were priced at mid × 1.10, so they could not fill. Notional is size × limit price.

## Short answers

| Probe | Order, notional under $10 | Result |
| --- | --- | --- |
| P9a | Reduce-only GTC sell, part of a 0.0051 long | Rejected: `Order must have minimum value of $10.` |
| P9b | Plain GTC sell, part of a 0.0051 long | Rejected, same error |
| P9c | Reduce-only IOC sell, part of a 0.0051 long | Rejected, same error |
| P9d | Reduce-only GTC sell of exactly a 0.0009 long | Accepted, rests |
| P9e | Reduce-only GTC sell of 0.01 against a 0.0009 long | Accepted, rests shrunk to 0.0009 |

The reduce-only flag does not exempt an order from the minimum. The time in force does not either.
An order sized to close the whole position is exempt, and so is a reduce-only order larger than the
position. Not tested: a plain GTC that closes the whole position under $10. P7 showed a plain IOC
does.

## P9a and P9b. Resting order for part of the position

Long 0.0051 ETH, mid 2568.2. Sell 0.0021 at 2825.0, notional 5.93.

Reduce-only GTC:

```json
{"status": "ok", "response": {"type": "order", "data": {"statuses": [{"error": "Order must have minimum value of $10. asset=4"}]}}}
```

Same order, not reduce-only:

```json
{"status": "ok", "response": {"type": "order", "data": {"statuses": [{"error": "Order must have minimum value of $10. asset=4"}]}}}
```

## P9c. IOC order for part of the position

Long 0.0051 ETH, mid 2569.1. Reduce-only IOC sell of 0.002 at mid × 0.95, notional about 4.88:

```json
{"status": "ok", "response": {"type": "order", "data": {"statuses": [{"error": "Order must have minimum value of $10. asset=4"}]}}}
```

The position stayed 0.0051. A reduce-only IOC sell of 0.0042 (notional about 10.25) then filled,
leaving a long of 0.0009 (position value 2.31).

## P9d. Resting order for the whole position

Long 0.0009 ETH. Reduce-only GTC sell of 0.0009 at 2826.0, notional 2.54:

```json
{"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 62088976113}}]}}}
```

`openOrders`:

```json
[{"coin": "ETH", "side": "A", "limitPx": "2826.0", "sz": "0.0009", "oid": 62088976113, "origSz": "0.0009", "reduceOnly": true}]
```

## P9e. Resting order larger than the position

Long 0.0009 ETH. Reduce-only GTC sell of 0.01 at 2826.0, requested notional 28.26:

```json
{"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 62088979389}}]}}}
```

`frontendOpenOrders`, cut to the fields that matter:

```json
[{"coin": "ETH", "side": "A", "limitPx": "2826.0", "sz": "0.0009", "origSz": "0.01", "reduceOnly": true, "orderType": "Limit", "tif": "Gtc"}]
```

Both resting orders were cancelled with `success`. The long was closed with a reduce-only IOC sell
of 0.0009. The account ended flat with no open orders.
