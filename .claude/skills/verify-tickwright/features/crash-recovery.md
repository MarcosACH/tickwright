# Crash recovery

The process dies with `KILL` after a fill. The next start rebuilds the order cache and the ledger
from the store, clears the barrier, and resumes the strategy seq from the saga high-water so no
signal id is ever reused.

## Sub-features

- `crash-durable` the fill, the position and the cash line survive a `KILL` (exit -9).
- `crash-converge` the second life restores the same rows and does not double-fill the order.
- `crash-seq` a new order in life two gets seq 2, never seq 1 again.

## How to get to it (user POV)

- `uv run tickwright`, `kill -9 <pid>` after the first fill, `uv run tickwright` again.

## Driving it with verify

Preconditions:

- Run id `crash` is unused.

- **Set up.** Run `$V init crash` and
  `$V ticks crash ticks --row BTC:42000@0 --row BTC:42100@1s`.
- **Start and fill.** Run
  `$V start crash first --preset paper-replay --env 'TICKWRIGHT_STRATEGIES=[{"kind":"single_shot_market","strategy_id":"shooter","symbol":"BTC","side":"buy","quantity":"0.5"}]'`
  and `$V await crash first --event order.filled`.
- **Crash.** Run `$V signal crash first KILL`, `$V await crash first --exit`,
  `$V dump crash first`. Exit code `-9`. `positions` shows `0.500 | 42000`. No
  `strategy_snapshots` were written (a crash takes none).
- **Restart.** Run the same `start` line with life `second`, then
  `$V await crash second --event engine.barrier_cleared`, `$V await crash second --event engine.feed_started`,
  wait two seconds, `$V signal crash second TERM`, `$V await crash second --exit`,
  `$V dump crash second`.
- **Proof.** The `orders` row for `shooter:BTC:1` is still `filled` with `cum_qty 0.500`, once.
  Any new order in life two carries `signal_id shooter:BTC:2`. Exit code `0`.
- **Cleanup.** Run `$V cleanup crash`.

## Gotchas

- **Known finding (2026-09-16).** After a `KILL` the shipped `single_shot_market` fires again on
  restart, because strategy snapshots are only taken on graceful stop and a crash leaves none.
  Life two shows `order.placed` for `shooter:BTC:2` and `positions` reads `1.000`. The saga is
  correct (no order is filled twice) but the strategy's own state is lost. ADR-0016 names a
  "periodic + on-stop" snapshot cadence that the runner does not implement. Report it as a
  product gap, do not paper over it in this file.
- The verification strategies in `portfolio-strategies.md` have the same property. Their phase
  is in the snapshot, and there is no snapshot after a crash.
- `KILL` leaves the pid file. `cleanup` handles it.
