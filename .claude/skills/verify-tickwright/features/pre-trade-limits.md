# Pre-trade limits

`TICKWRIGHT_LIMITS` sets four optional caps on the real guard (ADR-0051). A breach denies that one
order with a reason that names the cap. Nothing else changes, and the kill switch stays off. An
order that only reduces the position skips the size and value caps. The CLI allows one strategy
per symbol, so each cap gets its own life, and all six lives share one store.

## Sub-features

- `limits-size` a buy of 0.6 BTC with `max_order_size` 0.5 is denied.
- `limits-value` a buy of 0.45 BTC at a mark of 50000 (22500 USD) with `max_order_value` 20000
  is denied.
- `limits-inside` a buy of 0.4 BTC (20000 USD) fits every cap and fills.
- `limits-rate` with `rate_cap` 1 order in 5 seconds, a second order in the same life is denied,
  even on another symbol.
- `limits-position` with 0.8 BTC held and `max_position` 1, a buy of 0.4 is denied.
- `limits-close` a sell of 0.8 BTC closes the 0.8 held. It is above the size and value caps and
  still fills.
- `limits-no-double` after six lives on one store, no order is left open and exactly three filled.
- `limits-exit` every life stops with exit 0. A denial is not a fault.

## How to get to it (user POV)

- Set `TICKWRIGHT_GUARD=real` and `TICKWRIGHT_LIMITS` as one JSON value. `.env.example` has the
  reference.

## Driving it with verify

Preconditions:

- Run id `limits` is unused.

Every life uses the same two settings. Set them once in your shell:

```bash
G='TICKWRIGHT_GUARD=real'
L='TICKWRIGHT_LIMITS={"symbols":{"BTC":{"max_order_size":"0.5","max_order_value":"20000","max_position":"1"}},"mark_max_age_seconds":5,"rate_cap":{"max_orders":1,"window_seconds":5}}'
S() { echo "TICKWRIGHT_STRATEGIES=[{\"kind\":\"single_shot_market\",\"strategy_id\":\"$1\",\"symbol\":\"$2\",\"side\":\"$3\",\"quantity\":\"$4\"}]"; }
```

Every life ends the same way. For life `<x>`, run `$V await limits <x> --event order.denied` or
`--order ...=FILLED` as the step says, then `$V signal limits <x> TERM`,
`$V await limits <x> --exit`, `$V dump limits <x>`, and
`$V check limits <x> limits-exit --exit --expect 0`.

- **Set up.** Run `$V init limits --feature pre-trade-limits`. Write one tick file per life, each
  10 seconds after the last, so the store's clock only moves forward:
  `$V ticks limits a --row BTC:50000:1:buy@0 --row BTC:50000:1:sell@2s`,
  `$V ticks limits b --base 2024-01-01T00:00:10+00:00 --row BTC:50000:1:buy@0 --row BTC:50000:1:sell@2s`,
  `$V ticks limits c --base 2024-01-01T00:00:20+00:00 --row BTC:50000:1:buy@0 --row ETH:3000:1:sell@1s --row BTC:50000:1:buy@2s`,
  `$V ticks limits d --base 2024-01-01T00:00:30+00:00 --row BTC:50000:1:buy@0 --row BTC:50000:1:sell@2s`,
  `$V ticks limits e --base 2024-01-01T00:00:40+00:00 --row BTC:50000:1:buy@0 --row BTC:50000:1:sell@2s`,
  `$V ticks limits f --base 2024-01-01T00:00:50+00:00 --row BTC:50000:1:buy@0 --row BTC:50000:1:sell@2s`.
- **Size (a).** `$V start limits a --preset paper-replay --env TICKWRIGHT_REPLAY__PATH=a.jsonl --env "$G" --env "$L" --env "$(S s1 BTC buy 0.6)"`.
  Await `order.denied`. Check:
  `$V check limits a limits-size --sql "select reason from orders where strategy_id='s1'" --expect 'above max order size 0.5'`,
  `$V check limits a limits-size --sql "select state from orders where strategy_id='s1'" --expect denied`.
- **Value (b).** Same start with `b.jsonl` and `$(S s2 BTC buy 0.45)`. Await `order.denied`.
  Check:
  `$V check limits b limits-value --sql "select reason from orders where strategy_id='s2'" --expect 'above max order value 20000'`.
- **Rate (c).** Start with `c.jsonl` and
  `'TICKWRIGHT_STRATEGIES=[{"kind":"single_shot_market","strategy_id":"s3","symbol":"BTC","side":"buy","quantity":"0.4"},{"kind":"single_shot_market","strategy_id":"e3","symbol":"ETH","side":"buy","quantity":"0.1"}]'`.
  Await `--order $($V cloid s3:BTC:1)=FILLED` and `--event order.denied`. Check:
  `$V check limits c limits-inside --sql "select state from orders where strategy_id='s3'" --expect filled`,
  `$V check limits c limits-rate --sql "select reason from orders where strategy_id='e3'" --expect 'above max orders per window 1 in 5.0s'`.
- **Second fill (d).** Start with `d.jsonl` and `$(S s4 BTC buy 0.4)`. Await
  `--order $($V cloid s4:BTC:1)=FILLED`. The rate window starts empty on boot, so this order
  passes. BTC is now 0.8 across `s3` and `s4`. Check:
  `$V check limits d limits-inside --sql "select state from orders where strategy_id='s4'" --expect filled`.
- **Position (e).** Start with `e.jsonl` and `$(S s5 BTC buy 0.4)`. Await `order.denied`. The
  worst case is 0.8 + 0.4 = 1.2, above 1. Check:
  `$V check limits e limits-position --sql "select reason from orders where strategy_id='s5'" --expect 'above max position 1'`.
- **Close (f).** Start with `f.jsonl` and `$(S s6 BTC sell 0.8)`. Await
  `--order $($V cloid s6:BTC:1)=FILLED`. The sell is 0.8 coins and 40000 USD, above both caps. It
  moves the net from 0.8 to 0, so it only reduces. Check:
  `$V check limits f limits-close --sql "select state from orders where strategy_id='s6'" --expect filled`,
  `$V check limits f limits-close --sql "select sum(signed_size) from positions where symbol='BTC'" --expect 0`,
  `$V check limits f limits-close --sql "select cash from account" --expect 100000`,
  `$V check limits f limits-no-double --sql "select count(*) from orders where state not in ('filled','denied')" --expect 0`,
  `$V check limits f limits-no-double --sql "select count(*) from orders where state='filled'" --expect 3`.
- **Report.** Run `$V report limits`. Expected verdict: `PASS (0 of 18 checks failed)`.
  `REPORT.md` lists four `order.denied` alarms, one each for a, b, c, and e. Each one is the cap
  working.
- **Cleanup.** Run `$V cleanup limits`.

## Gotchas

- The CLI refuses two strategies on one symbol. That is why each cap gets its own life. Each life
  also gets a new `strategy_id`, so every signal id, and so every cloid, is unique on the store.
- The rate window lives in memory and empties on each boot. Prove the rate cap inside one life,
  with a second symbol, as in life c.
- Max position is the account net for the symbol, summed across strategies. The store keeps a row
  per strategy. After life f, `s3` and `s4` each hold 0.400 and `s6` holds -0.800. The net is 0,
  so check the sum, not one row.
- Every `LIMITS` symbol entry must name a traded symbol, or the engine refuses to start. So every
  life here trades BTC. ETH has no entry, and in life c only the rate cap applies to it.
- `guard` is `real` by default. The recipe sets it anyway, so the proof does not hang on a
  default. The two limits refusals (`noop` guard, an entry for an untraded symbol) are not driven
  here or in `config-refusals.md`.
- Under replay the mark is the trade price, published just ahead of each trade. So a market order
  is valued at 50000 here, and `mark_max_age_seconds` 5 never trips.
