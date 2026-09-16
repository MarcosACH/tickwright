# Crash recovery

The process dies with `KILL` after a fill. The next start rebuilds the order cache and the ledger
from the store, clears the barrier, resumes the strategy seq from the saga high-water so no
signal id is ever reused, and does not place a second order for a strategy that already fired.

## Sub-features

- `crash-durable` the fill, the position and the cash line survive a `KILL` (exit -9).
- `crash-converge` the second life restores the same rows: one filled order, position `0.500`,
  and places nothing.
- `crash-seq` no signal id is reused. `shooter:BTC:1` keys exactly one order.

## How to get to it (user POV)

- `uv run tickwright`, `kill -9 <pid>` after the first fill, `uv run tickwright` again.

## Driving it with verify

Preconditions:

- Run id `crash` is unused.
- `S='[{"kind":"single_shot_market","strategy_id":"shooter","symbol":"BTC","side":"buy","quantity":"0.5"}]'`

- **Set up.** Run `$V init crash --feature crash-recovery` and
  `$V ticks crash ticks --row BTC:42000@0 --row BTC:42100@1s`.
- **Life one, crash.** Run `$V start crash first --preset paper-replay --env "TICKWRIGHT_STRATEGIES=$S"`,
  `$V await crash first --event order.filled`, `$V signal crash first KILL`,
  `$V await crash first --exit`, `$V dump crash first`.
- **Check life one.** Run `$V check crash first crash-durable --exit --expect -9`,
  `$V check crash first crash-durable --sql "select signed_size from positions" --expect 0.500`,
  `$V check crash first crash-durable --sql "select count(*) from strategy_snapshots" --expect 0`.
- **Life two, restart.** Run the same `start` line with life `second`,
  `$V await crash second --event engine.feed_started`, wait two seconds,
  `$V signal crash second TERM`, `$V await crash second --exit`, `$V dump crash second`.
- **Check life two.** Run
  `$V check crash second crash-converge --sql "select count(*) from orders where state='filled'" --expect 1`,
  `$V check crash second crash-converge --sql "select signed_size from positions" --expect 0.500`,
  `$V check crash second crash-converge --event order.placed --expect 0`,
  `$V check crash second crash-seq --sql "select count(*) from orders where signal_id='shooter:BTC:1'" --expect 1`,
  `$V check crash second crash-seq --exit --expect 0`.
- **Report.** Run `$V report crash`. Expected verdict today: `FAIL (3 of 8 checks failed)`, all
  three on `crash-converge`. See Gotchas. When the engine snapshots strategies on a cadence, the
  verdict becomes `PASS`.
- **Cleanup.** Run `$V cleanup crash`.

## Gotchas

- **Known FAIL, tracked as #348.** After a `KILL` the shipped `single_shot_market` fires again
  on restart. Life two shows `order.placed` for `shooter:BTC:2`, two filled orders, and
  `positions` at `1.000`. Snapshots are only taken on graceful stop and a crash leaves none, so
  the strategy's `fired` flag is lost. The saga is correct (no order is filled twice) and the seq
  is correct (`shooter:BTC:2`, never `:1` again). ADR-0016 names a "periodic + on-stop"
  snapshot cadence that the runner does not implement. When #348 is fixed, the check flips to
  PASS and this note goes away.
- The verification strategies in `portfolio-strategies.md` have the same property.
- `KILL` leaves the pid file. `cleanup` handles it.
