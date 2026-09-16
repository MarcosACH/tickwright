# Paper round trip and restart

The quickstart path. An operator points the engine at a tick file, a market order fills on the
first tick, a low limit order rests, `Ctrl-C` stops the process with exit 0, and a second start
recovers the fill, places nothing new, and ghosts the resting order because the paper book died
with the first process.

## Sub-features

- `paper-fill` a `single_shot_market` fills on the first tick at the tick price.
- `paper-rest` a `single_shot_limit` below the market rests `LIVE`.
- `paper-stop` `TERM` exits 0, snapshots strategies, leaves the resting order alone.
- `paper-restart` the second life places nothing and exits 0.
- `paper-ghost` the second life rejects the resting order as a ghost on the first tick.

## How to get to it (user POV)

- `cp .env.example .env && uv run tickwright`, then `Ctrl-C`, then `uv run tickwright` again.

## Driving it with verify

Preconditions:

- `$V doctor` says the package imports.
- Run id `rt` is unused.
- `S='[{"kind":"single_shot_market","strategy_id":"shooter","symbol":"BTC","side":"buy","quantity":"0.5"},{"kind":"single_shot_limit","strategy_id":"rester","symbol":"ETH","side":"buy","quantity":"0.5","price":"2000"}]'`
- `FILL=$($V cloid shooter:BTC:1)` and `REST=$($V cloid rester:ETH:1)`

- **Set up.** Run `$V init rt --feature paper-round-trip` and
  `$V ticks rt ticks --row BTC:42000@0 --row ETH:2500@1s --row BTC:42100@2s`.
- **Life one.** Run `$V start rt first --preset paper-replay --env "TICKWRIGHT_STRATEGIES=$S"`,
  `$V await rt first --order $FILL=FILLED`, `$V await rt first --order $REST=LIVE`,
  `$V signal rt first TERM`, `$V await rt first --exit`, `$V dump rt first`.
- **Check life one.** Run
  `$V check rt first paper-fill --sql "select state from orders where cloid='$FILL'" --expect filled`,
  `$V check rt first paper-fill --sql "select signed_size from positions" --expect 0.500`,
  `$V check rt first paper-rest --sql "select state from orders where cloid='$REST'" --expect live`,
  `$V check rt first paper-stop --exit --expect 0`.
- **Life two.** Run `$V start rt second --preset paper-replay --env "TICKWRIGHT_STRATEGIES=$S"`,
  `$V await rt second --event engine.feed_started`, `$V await rt second --event ghost.reconciled --timeout 10`,
  `$V signal rt second TERM`, `$V await rt second --exit`, `$V dump rt second`.
- **Check life two.** Run
  `$V check rt second paper-restart --event order.placed --expect 0`,
  `$V check rt second paper-restart --exit --expect 0`,
  `$V check rt second paper-ghost --sql "select state from orders where cloid='$REST'" --expect rejected`.
- **Report.** Run `$V report rt`. Expected verdict: `PASS (0 of 7 checks failed)`.
- **Cleanup.** Run `$V cleanup rt`. Evidence stays in `.agents/verify/rt/evidence/`.

## Gotchas

- The engine does not exit when the tick file ends. Waiting on `--exit` without a signal hangs
  until the timeout.
- The resting order is ghosted in life two, not kept. The barrier leaves it `LIVE` and arms the
  grace window at virtual time 0. The first tick jumps the clock to 2024, so the 90 second
  window is already spent and the first open-order cycle rejects it. This is correct for the
  paper venue, whose book died with life one. See `ghost-reconcile.md` for the real-time window.
- `tests/app/test_cli_e2e.py` asserts no ghost in life two. It passes only because its ticks
  sit at `ts_event` 1000 ns, so the clock never moves past the window. Do not copy that
  expectation here.
- `isolated_collateral` in `positions` equals the notional at the default 1x isolated leverage.
  That is expected, not a bug.
