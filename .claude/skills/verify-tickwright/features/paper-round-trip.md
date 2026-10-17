# Paper round trip and restart

The quickstart path. An operator points the engine at a tick file, a market order fills on the
first tick, a low limit order rests, `Ctrl-C` stops the process with exit 0, and a second start
recovers the fill and places nothing new. Paper rebuilds its book from the store, so the resting
order is still `LIVE` in the second life (#484).

## Sub-features

- `paper-fill` a `single_shot_market` fills on the first tick at the tick price.
- `paper-rest` a `single_shot_limit` below the market rests `LIVE`.
- `paper-stop` `TERM` exits 0, snapshots strategies, leaves the resting order alone.
- `paper-restart` the second life places nothing and exits 0.
- `paper-keep` the second life keeps the resting order `LIVE` and ghosts nothing.

## How to get to it (user POV)

- `cp .env.example .env && uv run tickwright`, then `Ctrl-C`, then `uv run tickwright` again.
  `.env.example` runs one market strategy. Add a `single_shot_limit` entry to see a resting order.

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
  `$V await rt second --event engine.feed_started`, wait two seconds,
  `$V signal rt second TERM`, `$V await rt second --exit`, `$V dump rt second`.
- **Check life two.** Run
  `$V check rt second paper-restart --event order.placed --expect 0`,
  `$V check rt second paper-restart --exit --expect 0`,
  `$V check rt second paper-keep --sql "select state from orders where cloid='$REST'" --expect live`,
  `$V check rt second paper-keep --event ghost.reconciled --expect 0`.
- **Report.** Run `$V report rt`. Expected verdict: `PASS (0 of 8 checks failed)`.
- **Cleanup.** Run `$V cleanup rt`. Evidence stays in `.agents/verify/rt/evidence/`.

## Gotchas

- The engine does not exit when the tick file ends. Waiting on `--exit` without a signal hangs
  until the timeout.
- The resting order is kept in life two, not ghosted. Before the barrier, paper puts back every
  `LIVE` or partly filled GTC limit that the store holds (#484, `app/build.py`). A live venue
  keeps resting orders across a restart, and paper copies it. Before #484 this order was ghosted
  on the first tick. See `ghost-reconcile.md`.
- `isolated_collateral` in `positions` equals the notional at the default 1x isolated leverage.
  That is expected, not a bug.
