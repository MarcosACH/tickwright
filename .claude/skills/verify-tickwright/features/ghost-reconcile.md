# Ghost reconcile

A limit order rests `LIVE` in life one. The paper venue's book dies with the process, so in life
two the venue no longer holds it. The barrier does not conclude on one empty read. It arms the
90 second grace window, the open-order cadence re-reads every 30 seconds, and once the order has
been continuously absent across the window it is rejected as a ghost. The window is real on the
wall clock and already spent on the replay clock. Both are worth proving.

## Sub-features

- `ghost-armed` life two clears the barrier with the order still `LIVE` in the store.
- `ghost-wall-clock` on the Hyperliquid feed, `ghost.reconciled` lands 90 seconds after
  `engine.barrier_cleared`, not before.
- `ghost-replay` on the replay feed, the first tick with a real timestamp ghosts the order.
- `ghost-reject` the row reads `rejected`, reason `reconciliation: ghost: vanished from the venue`.

## How to get to it (user POV)

- Rest a limit, stop, start again, and wait a minute and a half on a live feed.

## Driving it with verify

Preconditions:

- Run ids `ghostlive` and `ghost` are unused. Network for the wall-clock case.
- `S='[{"kind":"single_shot_limit","strategy_id":"rester","symbol":"BTC","side":"buy","quantity":"0.001","price":"20000"}]'`
  for the live feed, `ETH` at price `2000` and quantity `0.5` for replay.

- **Wall clock, rest.** Run `$V init ghostlive --feature ghost-reconcile`,
  `$V start ghostlive first --preset paper-livefeed --env "TICKWRIGHT_STRATEGIES=$S"`,
  `$V await ghostlive first --order $($V cloid rester:BTC:1)=LIVE --timeout 90`,
  `$V signal ghostlive first TERM`, `$V await ghostlive first --exit`.
- **Wall clock, ghost.** Run the same `start` with life `second`,
  `$V await ghostlive second --event engine.barrier_cleared --timeout 60`, then
  `$V await ghostlive second --event ghost.reconciled --timeout 200`. Compare the two
  `timestamp` fields: the gap is 90 seconds plus at most one 30 second cadence
  (observed 90.0 s). Then `TERM`, `--exit`, `dump`. Check:
  `$V check ghostlive second ghost-armed --event engine.barrier_cleared --expect 1`,
  `$V check ghostlive second ghost-wall-clock --event ghost.reconciled --expect 1`,
  `$V check ghostlive second ghost-reject --sql "select state from orders" --expect rejected`,
  `$V check ghostlive second ghost-reject --event order.placed --expect 0`.
  Then `$V report ghostlive`, expected `PASS (0 of 4 checks failed)`, and `$V cleanup ghostlive`.
- **Replay, rest.** Run `$V init ghost --feature ghost-reconcile`,
  `$V ticks ghost first --row ETH:2500@0`,
  `$V ticks ghost second --base 2024-01-01T00:00:05+00:00 --row ETH:2502@0`,
  `$V start ghost first --preset paper-replay --env TICKWRIGHT_REPLAY__PATH=first.jsonl --env "TICKWRIGHT_STRATEGIES=$S"`,
  `$V await ghost first --order $($V cloid rester:ETH:1)=LIVE`, `TERM`, `--exit`.
- **Replay, ghost.** Same `start` with life `second` and `TICKWRIGHT_REPLAY__PATH=second.jsonl`,
  then `$V await ghost second --event ghost.reconciled --timeout 10`. It lands on the first
  tick. Then `TERM`, `--exit`, `dump`. Check:
  `$V check ghost second ghost-replay --event ghost.reconciled --expect 1`,
  `$V check ghost second ghost-reject --sql "select state from orders" --expect rejected`,
  `$V check ghost second ghost-reject --sql "select reason from orders" --expect "reconciliation: ghost: vanished from the venue"`.
- **Report.** Run `$V report ghost`. Expected verdict: `PASS (0 of 3 checks failed)`. The
  `ghost.reconciled` and `order.rejected` alarm lines are the feature, not a fault.
- **Cleanup.** Run `$V cleanup ghost`.

## Gotchas

- The replay clock starts at 0 in every life. The barrier arms the window at 0, and the first
  tick jumps to 2024, so the window is spent at once. This is why the replay case cannot measure
  the 90 seconds. Only the wall-clock case can. The in-process measurement on virtual time is
  `tests/engine/test_reconcile.py`.
- A recovered order has no "last event" in the new life, so the 30 second recent-order
  protection does not apply to it. Only the grace window guards it.
- The resting price must stay far from the market so the order is never filled instead. Under
  the live feed use 20000 for BTC.
- The `--base` of the second replay file must be later than the last row of the first.
