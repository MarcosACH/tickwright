# Kill switch

`USR1` trips a global halt. Every new `PlaceSignal` is denied while resting orders are left
alone. The halt is written to the store, so it survives a restart, and only `USR2` clears it.

## Sub-features

- `ks-trip` `USR1` logs `guard.kill_switch_tripped` and writes `kill_switch.tripped = 1`.
- `ks-deny` a strategy in a later life gets `order.denied` with reason `kill switch tripped`.
- `ks-durable` the denial happens in a fresh process on the same store.
- `ks-reset` `USR2` logs `guard.kill_switch_reset` and writes `tripped = 0`.

## How to get to it (user POV)

- `kill -USR1 <pid>` on a running engine, later `kill -USR2 <pid>`.

## Driving it with verify

Preconditions:

- Run id `ks` is unused.

- **Set up.** Run `$V init ks` and `$V ticks ks ticks --row BTC:42000@0 --row BTC:42100@1s`.
- **Arm.** Run `$V start ks arm --preset paper-replay`, `$V await ks arm --event engine.feed_started`,
  `$V signal ks arm USR1`, `$V await ks arm --event guard.kill_switch_tripped`,
  `$V signal ks arm TERM`, `$V await ks arm --exit`, `$V dump ks arm`. `kill_switch` reads
  `1 | 1 | SIGUSR1: operator kill switch | ...`.
- **Deny.** Run
  `$V start ks denied --preset paper-replay --env 'TICKWRIGHT_STRATEGIES=[{"kind":"single_shot_market","strategy_id":"shooter","symbol":"BTC","side":"buy","quantity":"0.5"}]'`
  and `$V await ks denied --event order.denied`. The line carries `"reason": "kill switch tripped"`
  and `"signal_id": "shooter:BTC:1"`.
- **Reset.** Run `$V signal ks denied USR2`, `$V await ks denied --event guard.kill_switch_reset`,
  `$V signal ks denied TERM`, `$V await ks denied --exit`, `$V dump ks denied`.
- **Proof.** `orders` has one row in state `denied` with reason `kill switch tripped`.
  `kill_switch` reads `1 | 0 | None | ...`. `positions` is empty.
- **Cleanup.** Run `$V cleanup ks`.

## Gotchas

- Under replay the ticks drain in milliseconds after `engine.feed_started`. A `USR1` sent to a
  life that has a strategy arrives after the order was placed. Trip the switch in a life with no
  strategy and let the durable row do the work in the next life, as above.
- `guard.kill_switch_tripped` and `guard.kill_switch_reset` carry no `run_id`. They are emitted
  from the signal handler, outside the run's context. Do not grep them by run id.
