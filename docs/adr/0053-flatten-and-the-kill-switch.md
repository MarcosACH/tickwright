# Flatten and the kill switch

An operator often trips the kill switch because something went wrong, then runs flatten. The kill
switch is durable, so the flatten run boots with it still tripped (ADR-0026). This ADR decides how
the two meet. Decided in [#411](https://github.com/MarcosACH/tickwright/issues/411), part of
[#408](https://github.com/MarcosACH/tickwright/issues/408).

## Decision

- **The kill switch never blocks a flatten order.** The kill switch exists to stop new exposure. A
  flatten order can only shrink a position. If the kill switch denied it, flatten would fail at the
  moment the operator needs it most.
- **Flatten trips the kill switch first.** The trip comes before the cancel step and before any
  order. A crash mid-flatten still leaves the store halted. The reason names the command, for
  example `"flatten: operator exit"`. Flatten trips it even when the account has nothing to close.
  The trip records what the operator meant, not what flatten found.
- **Flatten never resets the kill switch.** After a flatten, the next normal boot comes back
  halted. Strategies run, but every new order is denied until the operator sends `SIGUSR2`. The
  operator flattened because something went wrong. Trading again should be a separate choice.
- **Cancel all does not trip the kill switch.** An operator who cancels before a news event wants
  to quote again after it.
- **A tripped kill switch denies every strategy order, reduce-only included.** Strategy cancels
  still go out, because cancels never pass through the guard. During a halt, the operator's way out
  is to stop the engine and run flatten.
- **Under `GUARD=noop`, flatten runs anyway and warns.** `NoopGuard` has no kill switch, so it has
  nothing to trip. Flatten still closes positions. It emits a named event and a warning that the
  engine will trade as soon as it restarts.

## Considered options

- **Cancel all trips the kill switch too.** One rule for both commands. But it would force a halted
  restart on an operator who only paused quoting.
- **Flatten resets the kill switch when it is done.** The next boot would trade right away. But the
  operator may not have fixed what went wrong yet.
- **Allow strategy reduce-only orders during a halt.** A strategy could exit its own position. But
  the halt is often caused by a strategy. A buggy one could churn reduce-only orders, pay fees, and
  close positions at bad prices.
- **Refuse flatten under `GUARD=noop`.** This blocks the emergency exit to protect a restart the
  operator controls.
- **Move the kill switch out of the guard**, so it works under any guard. It is a bigger change
  than this effort needs. Choosing `noop` already turns off every pre-trade protection.

## Consequences

- Flatten needs a way to place orders that skip the kill switch check. How it does that depends on
  who owns a flatten order. That is still open in #408.
- Which pre-trade caps still apply to a flatten order is also still open in #408.
- The halted restart needs `GUARD=real` on the next boot too. `NoopGuard` never reads the stored
  kill switch. So a flatten under `GUARD=real` followed by a boot under `GUARD=noop` trades at once.

**(Resolved by ADR-0054:** how a flatten order skips the kill switch. A flatten order is owned by
the reserved id `__operator__`, and the guard skips the kill switch check for that owner. Decided in
[#412](https://github.com/MarcosACH/tickwright/issues/412).**)**
