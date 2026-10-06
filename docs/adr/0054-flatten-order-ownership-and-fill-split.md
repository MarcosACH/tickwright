# Who owns a flatten order and its fills

A flatten order comes from the operator, not from a strategy. But every order id is built from a
strategy id and a seq (ADR-0006). Every fill lands in the partition named by its order's strategy
(ADR-0038). This ADR decides who owns a flatten order, how its id is made, and where its fills land.
Decided in [#412](https://github.com/MarcosACH/tickwright/issues/412), part of
[#408](https://github.com/MarcosACH/tickwright/issues/408).

## Decision

- **Every partition reads flat after flatten.** That includes each strategy's partition and the
  unattributed one. If all fills landed in one partition, a strategy would still read a position on
  a flat account. Its PnL would be wrong. On the next boot, its exit logic would send a reduce-only
  order into a flat account.
- **One flatten order per symbol, sized to the account net.** It is reduce-only. Its owner is the
  reserved id `__operator__`.
- **Fills are split pro rata.** Each partition of the symbol moves the same share of the way to
  zero. Fees split the same way. Each split piece uses the real fill price.
- **The split runs on the total filled so far.** For each fill, the engine computes every
  partition's share of the order's cumulative fill, then books the change since the last fill. So
  rounding never leaves dust once the order is fully filled.
- **The partition sizes are read once, when the order is placed.** They are stored with the saga.
  A fill that arrives after a crash and restart splits against the same sizes.
- **The id is `__operator__:<symbol>:<seq>`.** The cloid is derived from it as usual (ADR-0006).
  The seq comes from the saga high-water for `__operator__`, the same way a strategy's does
  (ADR-0016). After a crash, the next flatten run finds the open saga by its id and resumes it. Only
  then does it place the next seq for what is left.
- **`__operator__` is reserved like `__unattributed__`.** Config and strategy registration refuse
  it as a strategy id. It never owns a partition. Its fills only feed the split.
- **The guard tells a flatten order by its owner.** An `__operator__` order skips the kill switch
  (ADR-0053).

Example. Strategy S holds BTC +2. The unattributed partition holds -1. The venue shows +1. Flatten
sends one reduce-only sell of 1. A fill of 0.5 books S selling 1 and the unattributed partition
buying 0.5, both at the fill price. The full fill leaves both at zero.

## Considered options

- **All fills land in one partition**, either `__unattributed__` or an operator partition. The
  account net reads zero, but each strategy keeps reading its old position.
- **One order per partition**, each owned by the partition it closes. Fills land the normal way.
  But when partitions have opposite signs, one of the orders must cross zero. It cannot be
  reduce-only, and the venue trades more than the net to close it.
- **Net the partitions first.** At flatten start, opposite partitions trade with each other inside
  the engine, at the mark. Only same-sign partitions are left for the venue order. But the internal
  trade books a price no one paid.
- **A new owner type on the order**, such as `Strategy | Operator`. It is more explicit. But it
  changes every reader of `Order.strategy_id`, where a reserved id follows a pattern the code
  already has.

## Consequences

- A flatten fill is the one fill that books into more than one partition. Every other fill still
  lands in the partition its order names.
- A fresh store restarts the `__operator__` seq at 1, so a flatten cloid can repeat an earlier
  life's cloid. The rules from #354 already cover this (ADR-0011).
- Foreign flow that arrives during a flatten run is healed into the unattributed partition after
  the order was placed. The stored sizes do not include it. The next flatten order covers what is
  left. The flatten saga prototype in #408 checks this case.
- Which pre-trade caps apply to an `__operator__` order is still open in #408.
