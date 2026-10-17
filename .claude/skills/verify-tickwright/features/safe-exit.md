# Safe exit

An operator gets out of the market in two one-shot commands. `tickwright cancel-all` pulls every
resting order in the account. `tickwright flatten` pulls them too, closes every position, and
trips the kill switch. Each exits 0 only when its last venue read finds nothing left (ADR-0060).
This recipe proves both on Hyperliquid testnet, and that the engine trades nothing after a
flatten until the operator resets the kill switch. A short paper run proves the store lock
refusal and a flatten with no key.

## Sub-features

- `se-setup` a strategy leaves a BTC long and a resting buy far below the market.
- `se-cancel-all` `cancel-all` exits 0, the resting buy ends `cancelled`, and the long stays.
- `se-flatten` `flatten` exits 0, every partition reads flat, and the kill switch is tripped.
- `se-halted` a restarted engine has its first order denied. The venue gets nothing.
- `se-reset` after `USR2`, the next life trades a round trip and ends flat.
- `se-redaction` the signing key appears nowhere in the evidence.
- `se-lock` on paper, `cancel-all` while the engine runs exits 2 with `exit.refused`.
- `se-paper-flatten` on paper replay, `flatten` exits 0, cancels the resting buy, closes the long,
  and trips the kill switch.

## How to get to it (user POV)

- Stop the engine with `TERM`. Then run `uv run tickwright cancel-all` or
  `uv run tickwright flatten` with the same `.env`. Each one ends by itself. Exit code 0 means
  done, 1 means it stopped partway, 2 means it never booted.
- After a flatten, start the engine as usual. It boots with the kill switch tripped. `kill -USR2`
  resets it.

## Driving it with verify

Preconditions:

- `$V doctor` says the repo `.env` has a signing key. The key is a funded testnet API wallet.
- `flatten` closes everything in the testnet account, not only this run's orders. Use an
  account that holds nothing else you want to keep.
- Run id `safe-exit` is unused. Budget about ten minutes.

- **Setup.** Run `$V init safe-exit --feature safe-exit` and
  `$V start safe-exit open --preset testnet --forward-key --strategy rest_then_hold --param QUANTITY=0.001 --param WAIT_SECONDS=10`.
  Run `$V await safe-exit open --event verify.rested --timeout 240`, then
  `$V await safe-exit open --order $($V cloid rest_then_hold:BTC:2)=LIVE --timeout 60`.
  Then `$V signal safe-exit open TERM`, `$V await safe-exit open --exit`, `$V dump safe-exit open`.
  Check at once, before the next life changes the store:
  `$V check safe-exit open se-setup --sql "select signed_size from positions where strategy_id='rest_then_hold'" --expect 0.001`,
  `$V check safe-exit open se-setup --sql "select state from orders where cloid='$($V cloid rest_then_hold:BTC:2)'" --expect live`.
- **Cancel all.** Run `$V start safe-exit cancel --preset testnet --forward-key --command cancel-all`,
  then `$V await safe-exit cancel --exit --timeout 120` and `$V dump safe-exit cancel`. Check
  `$V check safe-exit cancel se-cancel-all --exit --expect 0`,
  `$V check safe-exit cancel se-cancel-all --sql "select state from orders where cloid='$($V cloid rest_then_hold:BTC:2)'" --expect cancelled`,
  `$V check safe-exit cancel se-cancel-all --sql "select signed_size from positions where strategy_id='rest_then_hold'" --expect 0.001`.
- **Flatten.** Run `$V start safe-exit flatten --preset testnet --forward-key --command flatten`,
  then `$V await safe-exit flatten --exit --timeout 180` and `$V dump safe-exit flatten`. Check
  `$V check safe-exit flatten se-flatten --exit --expect 0`,
  `$V check safe-exit flatten se-flatten --sql "select count(*) from positions where cast(signed_size as real) != 0" --expect 0`,
  `$V check safe-exit flatten se-flatten --sql "select tripped from kill_switch" --expect 1`.
- **Halted.** Run `$V start safe-exit halted --preset testnet --forward-key --strategy margin_watch --param QUANTITY=0.001`,
  then `$V await safe-exit halted --event order.denied --timeout 240`.
  Send `$V signal safe-exit halted USR2` and `$V await safe-exit halted --event guard.kill_switch_reset`.
  Then `$V signal safe-exit halted TERM`, `$V await safe-exit halted --exit`, `$V dump safe-exit halted`.
  Check `$V check safe-exit halted se-halted --event order.denied --expect 1`,
  `$V check safe-exit halted se-halted --sql "select count(*) from orders where strategy_id='margin_watch' and venue_oid is not null" --expect 0`,
  `$V check safe-exit halted se-reset --sql "select tripped from kill_switch" --expect 0`.
- **Reset.** Run `$V start safe-exit reset --preset testnet --forward-key --strategy round_trip_then_flat --param QUANTITY=0.001 --param HOLD_TICKS=1 --param WAIT_SECONDS=10`,
  then `$V await safe-exit reset --event verify.flat --timeout 240`.
  Then `$V signal safe-exit reset TERM`, `$V await safe-exit reset --exit`, `$V dump safe-exit reset`,
  and `$V secrets-check safe-exit`. Check
  `$V check safe-exit reset se-reset --event verify.flat --expect 1`,
  `$V check safe-exit reset se-reset --sql "select count(*) from positions where cast(signed_size as real) != 0" --expect 0`,
  `$V check safe-exit reset se-redaction --event verify.flat --expect 1` (the run reached the end)
  and read the `secrets-check` line: `no signing key in evidence`.
- **Report.** Run `$V report safe-exit`. Expected verdict: `PASS`. A FAIL that is not a Gotcha
  below is new, and `$V issue-draft safe-exit` drafts it.
- **Cleanup.** Run `$V cleanup safe-exit`.
- **Paper, set up.** In a separate run, run `$V init se-paper --feature safe-exit`,
  `$V ticks se-paper ticks --row BTC:42000@0 --row BTC:42100@1s --row BTC:42200@2s`,
  `$V start se-paper held --preset paper-replay --strategy rest_then_hold --param QUANTITY=0.5`,
  `$V await se-paper held --event verify.rested`, and
  `$V await se-paper held --order $($V cloid rest_then_hold:BTC:2)=LIVE`. Leave it running.
- **Paper, lock.** While `held` runs, run
  `$V start se-paper lock --preset paper-replay --command cancel-all` and
  `$V await se-paper lock --exit`. Check
  `$V check se-paper lock se-lock --exit --expect 2`,
  `$V check se-paper lock se-lock --event exit.refused --expect 1`. The line reads
  `reason: lock_held` and names the holder's pid. Then `$V signal se-paper held TERM`,
  `$V await se-paper held --exit`, `$V dump se-paper held`.
- **Paper, flatten.** Run `$V start se-paper flatten --preset paper-replay --command flatten`,
  `$V await se-paper flatten --exit --timeout 60`, `$V dump se-paper flatten`. Check
  `$V check se-paper flatten se-paper-flatten --exit --expect 0`,
  `$V check se-paper flatten se-paper-flatten --sql "select state from orders where cloid='$($V cloid rest_then_hold:BTC:2)'" --expect cancelled`,
  `$V check se-paper flatten se-paper-flatten --sql "select count(*) from positions where cast(signed_size as real) != 0" --expect 0`,
  `$V check se-paper flatten se-paper-flatten --sql "select tripped from kill_switch" --expect 1`.
  The store holds one `__operator__` market sell of 0.5. Then `$V report se-paper`, expected
  `PASS (0 of 6 checks failed)`, and `$V cleanup se-paper`.

## Gotchas

- Every life shares one store, and `check --sql` reads it as it is now. Run each life's checks
  before the next life starts, or they read a later state.
- **Wait for the first mark.** On Hyperliquid, a market order sent before the first mark arrives
  is never sent. It ends `failed` with reason `no mark cached for 'BTC'`, and the engine keeps
  running ([#509](https://github.com/MarcosACH/tickwright/issues/509)). The checks need the order
  to trade. `WAIT_SECONDS=10` makes the strategy wait 10 seconds of wall time after its first
  trade, so a mark arrives. The `halted` life does not need it, because the guard denies its
  order before the adapter prices it.
- Exit code 0 from an exit run is the venue proof. Only a final venue read with nothing left
  makes a run `done` (ADR-0060). The `exit.finished` line names what was left when it is not 0.
- `rest_then_hold` rests at `REST_BELOW` (default 0.8) times the last trade. Hyperliquid refuses
  a limit price too far from the reference price, so do not go much lower.
- The resting order has seq 2 because the market buy took seq 1. If the buy is split into
  partial fills, the rest still waits for the order's final fill.
- `halted` uses `margin_watch` on purpose. A denied strategy keeps its `buying` phase in its
  snapshot, so the `reset` life uses a different strategy id to start clean.
- `feed.lagged` lines are normal on testnet. The stream conflates.
- The paper `cancel-all` and `flatten` see the resting buy because paper rebuilds its book from
  the store at boot (#484). A replay flatten plays the whole tick file and fills at the last row
  (ADR-0059).
- `start` cannot pass `--new-store`, so the `new_store` refusal is not driven here.
