# Paper round trip and restart

The quickstart path. An operator points the engine at a tick file, a market order fills on the
first tick, a low limit order rests, `Ctrl-C` stops the process with exit 0, and a second start
recovers the resting order without placing anything new.

## Sub-features

- `paper-fill` a `single_shot_market` fills on the first tick at the tick price.
- `paper-rest` a `single_shot_limit` below the market rests `LIVE`.
- `paper-stop` `TERM` exits 0, snapshots strategies, leaves the resting order alone.
- `paper-restart` the second life clears the barrier with the order still `LIVE`, places
  nothing, and ghosts the order on the first tick (the paper book died with life one).

## How to get to it (user POV)

- `cp .env.example .env && uv run tickwright`, then `Ctrl-C`, then `uv run tickwright` again.

## Driving it with verify

Preconditions:

- `$V doctor` says the package imports.
- Run id `rt` is unused.

- **Set up.** Run `$V init rt` and
  `$V ticks rt ticks --row BTC:42000@0 --row ETH:2500@1s --row BTC:42100@2s`.
- **Start life one.** Run
  `$V start rt first --preset paper-replay --env 'TICKWRIGHT_STRATEGIES=[{"kind":"single_shot_market","strategy_id":"shooter","symbol":"BTC","side":"buy","quantity":"0.5"},{"kind":"single_shot_limit","strategy_id":"rester","symbol":"ETH","side":"buy","quantity":"0.5","price":"2000"}]'`.
- **Fill.** Run `$V await rt first --order $($V cloid shooter:BTC:1)=FILLED`. The row appears.
- **Rest.** Run `$V await rt first --order $($V cloid rester:ETH:1)=LIVE`. The row appears.
- **Stop.** Run `$V signal rt first TERM`, `$V await rt first --exit`, `$V dump rt first`. Exit
  code `0`. `positions` shows `shooter | BTC | 0.500 | 42000`. `account` cash is `100000.000`
  (frictionless). `orders` shows `filled` and `live`.
- **Restart.** Run the same `start` line with life `second`, then
  `$V await rt second --event engine.barrier_cleared`, `$V await rt second --event engine.feed_started`,
  `$V signal rt second TERM`, `$V await rt second --exit`, `$V dump rt second`.
- **Proof.** `second.store.txt` has exactly the same two cloids. The market order is still
  `filled` once. The limit order reads `rejected` with reason
  `reconciliation: ghost: vanished from the venue`. `second.events.txt` has no `order.placed`,
  one `engine.barrier_cleared`, one `ghost.reconciled`. `second.exit` is `0`.
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
