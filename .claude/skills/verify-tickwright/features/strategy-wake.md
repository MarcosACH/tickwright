# Strategy wake-ups

A strategy can wake on a clock grid, even when ticks stop (ADR-0062). The reference limit strategy
uses it to cancel its resting order after N seconds. This recipe proves the cancel with no tick on
the order's symbol.

## Sub-features

- `wake-cancel` the resting BTC limit is cancelled after `cancel_after_seconds`, with no BTC tick
  after it was placed. ETH rows move the replay clock.
- `wake-deadline` the same order stays `LIVE` when replay time stops before the deadline.

## How to get to it (user POV)

- Add `"cancel_after_seconds": 3` to a `SingleShotLimitStrategy` entry's `settings` in
  `TICKWRIGHT_STRATEGIES`, then `uv run tickwright`. The order is cancelled 3 seconds after its
  placing tick, whether or not more ticks arrive.

## Driving it with verify

Preconditions:

- `$V doctor` says the package imports.
- Run ids `wake` and `wakectl` are unused.
- `S='[{"strategy":"tickwright.strategies.single_shot_limit:SingleShotLimitStrategy","strategy_id":"timer","symbols":["BTC"],"settings":{"side":"buy","quantity":"0.5","price":"40000","cancel_after_seconds":3}}]'`
- `REST=$($V cloid timer:BTC:1)`

- **Set up.** Run `$V init wake --feature strategy-wake`, `$V init wakectl --feature strategy-wake`,
  `$V ticks wake ticks --row BTC:42000@300ms --row ETH:2500@1300ms --row ETH:2500@2300ms --row ETH:2500@3300ms --row ETH:2500@4300ms --row ETH:2500@5300ms`
  and `$V ticks wakectl ticks --row BTC:42000@300ms --row ETH:2500@1300ms --row ETH:2500@2300ms`.
- **Cancel.** Run `$V start wake first --preset paper-replay --env "TICKWRIGHT_STRATEGIES=$S"`,
  `$V await wake first --order $REST=CANCELLED`, `$V signal wake first TERM`,
  `$V await wake first --exit`, `$V dump wake first`.
- **Control.** Run `$V start wakectl first --preset paper-replay --env "TICKWRIGHT_STRATEGIES=$S"`,
  `$V await wakectl first --order $REST=LIVE`, wait three seconds, `$V signal wakectl first TERM`,
  `$V await wakectl first --exit`, `$V dump wakectl first`.
- **Check.** Run
  `$V check wake first wake-cancel --sql "select state from orders where cloid='$REST'" --expect cancelled`,
  `$V check wake first wake-cancel --exit --expect 0`,
  `$V check wakectl first wake-deadline --sql "select state from orders where cloid='$REST'" --expect live`,
  `$V check wakectl first wake-deadline --event order.cancelled --expect 0`.
- **Report.** Run `$V report wake` and `$V report wakectl`. Expected verdict for each:
  `PASS (0 of 2 checks failed)`.
- **Cleanup.** Run `$V cleanup wake` and `$V cleanup wakectl`.

## Gotchas

- Replay time moves only when a row is read. When the file ends, time stops and no wake fires. So
  the ETH rows are what carry time past the deadline. The order's own symbol gets no tick.
- The deadline is the placing tick's time plus N seconds. Here that is 3.3 s. The first wake at or
  past it is the 4 s slot, so the ETH row at 4.3 s is the one that fires the cancel.
- Log timestamps are wall time, not replay time. That is why the control run proves the deadline,
  not a log line.
- The control is its own run id. `check --sql` reads the store named in the run's `scratch/.env`,
  and each `start` rewrites that file.
