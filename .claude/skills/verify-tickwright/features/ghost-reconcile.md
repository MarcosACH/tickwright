# Ghost reconcile

A ghost is a resting order the store holds but the venue does not. The barrier never concludes on
one empty read. It arms a 90 second grace window, the open-order cadence re-reads every 30
seconds, and an order absent for the whole window ends `rejected` as a ghost. Since #484, paper
rebuilds its book from the store at boot, so a plain restart no longer makes a ghost. This file
proves that a restart keeps the order, on both clocks. The helper cannot make a real ghost today.

## Sub-features

- `ghost-keep-replay` on the replay feed, a restart keeps the resting order `LIVE` and ghosts
  nothing.
- `ghost-keep-wall-clock` on the Hyperliquid feed, the order is still `LIVE` two minutes after
  `engine.barrier_cleared`. That is past the 90 second window.
- `ghost-reject` verified-unreachable. A ghost needs an order the venue has no record of. Paper
  now keeps every resting order, and no helper verb removes an order from a venue.
  `tests/engine/test_reconcile.py` proves the window, the reject, and the reason
  `reconciliation: ghost: vanished from the venue` in process.

## How to get to it (user POV)

- Rest a limit, stop, and start again. The order is still `LIVE`, and the venue can still fill
  it.

## Driving it with verify

Preconditions:

- Run ids `ghostlive` and `ghost` are unused. Network for the wall-clock case.
- `S='[{"kind":"single_shot_limit","strategy_id":"rester","symbol":"BTC","side":"buy","quantity":"0.001","price":"20000"}]'`
  for the live feed, `ETH` at price `2000` and quantity `0.5` for replay.

- **Wall clock, rest.** Run `$V init ghostlive --feature ghost-reconcile`,
  `$V start ghostlive first --preset paper-livefeed --env "TICKWRIGHT_STRATEGIES=$S"`,
  `$V await ghostlive first --order $($V cloid rester:BTC:1)=LIVE --timeout 90`,
  `$V signal ghostlive first TERM`, `$V await ghostlive first --exit`.
- **Wall clock, keep.** Run the same `start` with life `second`,
  `$V await ghostlive second --event engine.barrier_cleared --timeout 60`, then wait two
  minutes. Then `TERM`, `--exit`, `dump`. Check:
  `$V check ghostlive second ghost-keep-wall-clock --sql "select state from orders" --expect live`,
  `$V check ghostlive second ghost-keep-wall-clock --event ghost.reconciled --expect 0`,
  `$V check ghostlive second ghost-keep-wall-clock --event order.placed --expect 0`.
  Then `$V report ghostlive`, expected `PASS (0 of 3 checks failed)`, and `$V cleanup ghostlive`.
- **Replay, rest.** Run `$V init ghost --feature ghost-reconcile`,
  `$V ticks ghost first --row ETH:2500@0`,
  `$V ticks ghost second --base 2024-01-01T00:00:05+00:00 --row ETH:2502@0 --row ETH:2501@2m`,
  `$V start ghost first --preset paper-replay --env TICKWRIGHT_REPLAY__PATH=first.jsonl --env "TICKWRIGHT_STRATEGIES=$S"`,
  `$V await ghost first --order $($V cloid rester:ETH:1)=LIVE`, `TERM`, `--exit`.
- **Replay, keep.** Same `start` with life `second` and `TICKWRIGHT_REPLAY__PATH=second.jsonl`,
  then `$V await ghost second --event engine.feed_started` and wait three seconds. The replay
  clock starts at 0, and the first row jumps it to 2024, so the 90 second window is spent at
  once. The second row gives reconcile two more minutes. Then `TERM`, `--exit`, `dump`. Check:
  `$V check ghost second ghost-keep-replay --sql "select state from orders" --expect live`,
  `$V check ghost second ghost-keep-replay --event ghost.reconciled --expect 0`.
- **Report.** Run `$V report ghost`. Expected verdict: `PASS (0 of 2 checks failed)`.
- **Cleanup.** Run `$V cleanup ghost`.

## Gotchas

- Paper puts back only `LIVE` or partly filled GTC limit rows that hold a price (`_resting_orders`
  in `app/build.py`). A row from before the store kept order terms has no price. Paper cannot
  rest it, so reconcile still ghosts it. The helper cannot make such a row.
- Before #484 this file proved a ghost on paper. Older runs in `$V runs` that read `PASS` on
  `ghost-replay` or `ghost-wall-clock` proved that old behavior.
- The resting price must stay far from the market so the order is never filled instead. Under
  the live feed use 20000 for BTC.
- The `--base` of the second replay file must be later than the last row of the first.
