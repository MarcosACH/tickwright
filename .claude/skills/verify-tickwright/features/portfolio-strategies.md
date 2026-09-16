# Portfolio strategies

Four throwaway strategies in `scripts/strategies.py` read the `Portfolio` seam the way a real
strategy would and log what they read as `verify.*` events. They prove the accounting surface
from the strategy's side of the seam: positions return to flat, unrealized PnL drives a decision,
margin math holds at leverage, and two strategies see their own partitions but one account.

## Sub-features

- `ps-round-trip` `round_trip_then_flat`: buy, hold N ticks, sell. Flat with the right realized.
- `ps-take-profit` `take_profit`: sell when `unrealized_pnl >= TAKE_PROFIT`.
- `ps-margin` `margin_watch`: buy at 5x cross, read equity, margin used, free margin, liquidation
  price through a crash. Free margin goes negative and nothing closes the position.
- `ps-dual` `dual_symbol`: BTC long and ETH short from two strategies. Each sees one partition,
  both read one account equity.

## How to get to it (user POV)

- `$V start <run> <life> --preset paper-replay --strategy <name> --param KEY=VALUE`. Params:
  `SYMBOL` (BTC), `SYMBOL2` (ETH), `QUANTITY` (0.5), `HOLD_TICKS` (2), `TAKE_PROFIT` (100),
  `LEVERAGE` (JSON, `{}`).

## Driving it with verify

Preconditions:

- Run id `strat` is unused. `jq` or `python3` to read one JSON line.

- **Round trip.** Run `$V init strat --feature portfolio-strategies`,
  `$V ticks strat rt --row BTC:42000@0 --row BTC:42100@1s --row BTC:42200@2s --row BTC:42300@3s`,
  `$V start strat rt --preset paper-replay --strategy round_trip_then_flat --param HOLD_TICKS=2 --env TICKWRIGHT_REPLAY__PATH=rt.jsonl`,
  `$V await strat rt --event verify.flat`, `$V signal strat rt TERM`, `$V await strat rt --exit`,
  `$V dump strat rt`. Check:
  `$V check strat rt ps-round-trip --sql "select signed_size from positions" --expect 0.000`,
  `$V check strat rt ps-round-trip --sql "select realized_pnl from positions" --expect 100.000`
  (0.5 × (42200 − 42000)),
  `$V check strat rt ps-round-trip --sql "select count(*) from orders where state='filled'" --expect 2`.
- **Take profit.** Run `rm .agents/verify/strat/scratch/store.db`,
  `$V ticks strat tp --row BTC:42000@0 --row BTC:42050@1s --row BTC:42150@2s --row BTC:42400@3s --row BTC:42500@4s`,
  `$V start strat tp --preset paper-replay --strategy take_profit --param QUANTITY=1 --param TAKE_PROFIT=300 --env TICKWRIGHT_REPLAY__PATH=tp.jsonl`,
  `$V await strat tp --event verify.done`, `$V signal strat tp TERM`, `$V await strat tp --exit`,
  `$V dump strat tp`. The `verify.tick` lines show `upnl` 50, 150, 400. The sell fires at 400.
  Check: `$V check strat tp ps-take-profit --sql "select realized_pnl from positions" --expect 400.000`,
  `$V check strat tp ps-take-profit --sql "select cash from account" --expect 100400.000`,
  `$V check strat tp ps-take-profit --event verify.tick --expect 3`.
- **Margin.** Run `rm .agents/verify/strat/scratch/store.db`,
  `$V ticks strat margin --row BTC:40000@0 --row BTC:40000@1s --row BTC:32000@2s --row BTC:30000@3s`,
  `$V start strat margin --preset paper-replay --strategy margin_watch --param QUANTITY=10 --param 'LEVERAGE={"BTC": {"mode": "cross", "leverage": 5}}' --env TICKWRIGHT_REPLAY__PATH=margin.jsonl`,
  `$V await strat margin --event verify.tick --count 3`, `$V signal strat margin TERM`,
  `$V await strat margin --exit`, `$V dump strat margin`. The third `verify.tick` line, at mark
  30000, reads `margin_used 60000.000` (300000 / 5), `unrealized_pnl -100000.000`,
  `equity 0.000`, `free_margin -60000.000`, `liquidation_price` about 30612. Check:
  `$V check strat margin ps-margin --sql "select signed_size from positions" --expect 10.000`
  (still open, nothing liquidated),
  `$V check strat margin ps-margin --sql "select count(*) from orders" --expect 1`,
  `$V check strat margin ps-margin --event verify.tick --expect 3`.
- **Dual.** Run `rm .agents/verify/strat/scratch/store.db`,
  `$V ticks strat dual --row BTC:42000@0 --row ETH:2500@1s --row BTC:42100@2s --row ETH:2450@3s`,
  `$V start strat dual --preset paper-replay --strategy dual_symbol --param QUANTITY=1 --env TICKWRIGHT_REPLAY__PATH=dual.jsonl`,
  `$V await strat dual --event verify.tick --count 2`, `$V signal strat dual TERM`,
  `$V await strat dual --exit`, `$V dump strat dual`. In the log, `leg_long`'s `open` lists
  only BTC with upnl 100, `leg_short`'s only ETH with upnl 50, and the second
  `account.equity` reads `100150.000`. Check:
  `$V check strat dual ps-dual --sql "select signed_size from positions where strategy_id='leg_long'" --expect 1.000`,
  `$V check strat dual ps-dual --sql "select signed_size from positions where strategy_id='leg_short'" --expect -1.000`,
  `$V check strat dual ps-dual --sql "select count(*) from positions" --expect 2`.
- **Report.** Run `$V report strat`. Expected verdict: `PASS (0 of 12 checks failed)`.
- **Cleanup.** Run `$V cleanup strat`.

## Gotchas

- These strategies are registered by hand, so `TICKWRIGHT_STRATEGIES` must be `[]` (the presets
  set it) and `TICKWRIGHT_LEVERAGE` cannot be used. Pass `--param LEVERAGE=...` instead.
- `max_leverage` in the preset specs is 20. A `LEVERAGE` above it refuses at `start()`.
- The strategies are single-flight. A tick that arrives while an order is in flight is ignored
  by design, so count ticks from the fill, not from the file.
- The mark is the last trade price. `unrealized_pnl` is `null` until the first tick after the
  fill under the live feed, and `0` on the fill tick under replay.
