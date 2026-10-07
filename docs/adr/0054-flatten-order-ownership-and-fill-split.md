# Who owns a flatten order and its fills

A flatten order comes from the operator, not from a strategy. But every order id is built from a
strategy id and a seq (ADR-0006). Every fill lands in the partition named by its order's strategy
(ADR-0038). This ADR decides who owns a flatten order, how its id is made, and where its fills land.
Decided in [#412](https://github.com/MarcosACH/tickwright/issues/412), part of
[#408](https://github.com/MarcosACH/tickwright/issues/408).

## Decision

- **Every strategy partition reads flat after flatten.** If all fills landed in one partition, a
  strategy would still read a position on a flat account. Its PnL would be wrong. On the next boot,
  its exit logic would send a reduce-only order into a flat account. The unattributed partition
  reads flat too, unless the venue still holds dust or the books disagree with a flat venue. Both
  cases are below.
- **One flatten order per symbol, sized to the venue's position.** The size is read from the venue
  when the order is placed. It is reduce-only. Its owner is the reserved id `__operator__`.
  **(Amended by ADR-0056:** one order at a time per symbol, not one in total. Each attempt is a
  market order. When it ends and the venue still holds a position, flatten reconciles and places
  the next seq for what is left. Decided in
  [#413](https://github.com/MarcosACH/tickwright/issues/413).**)**
- **The order is placed only when the books match the venue.** The split moves the partitions by
  the filled size in total. So the partition sizes must sum to the venue size, or the books drift
  from the venue. If they differ, flatten waits for reconciliation to heal the gap into the
  unattributed partition (ADR-0038). A flat venue is the exception. There is no order to place, so
  the leftover rule below applies.
- **Fills are split pro rata.** Each partition of the symbol moves the same share of the way to
  zero. Each split piece uses the real fill price.
- **Fees split by the absolute size each partition books.** Partitions can have opposite signs. A
  split by signed size would give one of them a negative fee. The absolute weights still sum to the
  real fee.
- **The split runs on the total filled so far.** For each fill, the engine computes every
  partition's share of the order's cumulative fill, then books the change since the last fill. So
  rounding never leaves dust once the order is fully filled.
  **(Amended by ADR-0056:** an attempt is IOC, so it often ends partly filled. ADR-0056 sets the
  rounding rule. The moves sum to the fill after every fill, and a later fill never takes a lot
  back. Decided in [#413](https://github.com/MarcosACH/tickwright/issues/413).**)**
- **The partition sizes are read once, when the order is placed.** They are stored with the saga.
  A fill that arrives after a crash and restart splits against the same sizes.
- **The id is `__operator__:<symbol>:<seq>`.** The cloid is derived from it as usual (ADR-0006).
  The seq comes from the saga high-water for `__operator__`, the same way a strategy's does
  (ADR-0016). After a crash, the next flatten run finds the open saga by its id and resumes it. Only
  then does it place the next seq for what is left.
- **When no venue order can close a strategy's leftover, it moves into the unattributed
  partition.** This happens when the venue holds no position in the symbol, or when the venue
  refuses the order as too small. The leftover moves at the strategy's own average entry price, so its
  realized PnL does not change. No trade happens at the venue, and no fee is booked. When the
  venue refuses a too-small order, the unattributed partition keeps that dust. So the sum still
  equals the venue size.
  **(Narrowed by ADR-0058:** the venue minimum never refuses a flatten order, because each attempt
  closes the whole venue position. No other venue check is known to refuse one as too small. So
  the too-small case is a fallback only. Decided in
  [#435](https://github.com/MarcosACH/tickwright/issues/435).**)**
- **`__operator__` is reserved like `__unattributed__`.** Config and strategy registration refuse
  it as a strategy id. It never owns a partition. Its fills only feed the split.
- **The guard skips the kill switch only for an `__operator__` order that is reduce-only.**
  ADR-0053 lets flatten skip it because a flatten order can only shrink a position. That comes from
  reduce-only, not from the owner. So the guard checks both, and a tripped kill switch denies any
  other `__operator__` order.

Example. Strategy S holds BTC +2. The unattributed partition holds -1. The venue shows +1. Flatten
sends one reduce-only sell of 1. A fill of 0.5 books S selling 1 and the unattributed partition
buying 0.5, both at the fill price. The fill pays a fee of 0.03. S books 1 of the 1.5 total, so it
pays 0.02. The unattributed partition pays 0.01. The full fill leaves both at zero.

Example with a flat venue. Strategy S buys BTC +1. The operator sells 1 by hand in the venue UI,
so the venue shows 0. Reconciliation cannot book that sale. Its size heal is priced at the venue's
entry price, and the venue gives none for a flat position. So the books still hold S at +1, and
flatten has no order to send. S's +1 moves into the unattributed partition at S's entry price. S
reads flat, and its realized PnL is unchanged. The unattributed +1 stays a visible size finding, as
the gap was before flatten.

## Considered options

- **Size the order to the sum of the book partitions.** When the books hold less than the venue,
  the order leaves venue exposure open while every partition reads flat.
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
- **For a leftover no order can close, leave the partitions and warn.** The strategy would wake up
  holding a position the venue does not have. This is the failure this ADR exists to stop.
- **For a leftover no order can close, move every partition to zero at the mark.** It is simple,
  but it books a price no one paid. Moving the leftover at the strategy's own entry price invents
  no price.
- **For a leftover no order can close, send one venue order per partition.** On a flat account,
  closing S's +1 means a real sell that opens a short, then a buy that closes it. That is two fees,
  real exposure in between, and orders that cannot be reduce-only.

## Consequences

- A flatten fill is the one fill that books into more than one partition. Every other fill still
  lands in the partition its order names.
- The leftover move is a new ledger write. It touches two positions of one symbol and no order.
- The split and the move keep the book's cost basis, so they open no cash gap of their own. The
  next reconcile still sets cash to the venue's figure, so any drift heals there (ADR-0034).
- A fresh store restarts the `__operator__` seq at 1, so a flatten cloid can repeat an earlier
  life's cloid. The rules from #354 already cover this (ADR-0011).
- Foreign flow that arrives during a flatten run is healed into the unattributed partition after
  the order was placed. The stored sizes do not include it. The next flatten order covers what is
  left. The flatten saga prototype in #408 checks this case.
  **(Resolved by ADR-0056:** the prototype confirmed it. A hand trade that grows the position is
  closed by the next attempt. One that closes the venue ends flatten, and the leftover rule keeps
  the gap in the unattributed partition. Decided in
  [#413](https://github.com/MarcosACH/tickwright/issues/413).**)**
- Which pre-trade caps apply to an `__operator__` order is still open in #408.
- How long flatten waits for the books to match the venue is still open in #408. So is what
  flatten does when they never match.
  **(Resolved by ADR-0056:** flatten never waits. After reconcile, the books match the venue,
  unless the venue is flat. Then the leftover rule applies. A venue read that fails is still open
  in #408. Decided in [#413](https://github.com/MarcosACH/tickwright/issues/413).**)**
