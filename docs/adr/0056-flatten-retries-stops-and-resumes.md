# How flatten retries, stops, and resumes after a crash

A flatten order can fill only partly. The position can change while flatten runs. The engine can
crash halfway. ADR-0054 decides who owns a flatten order and how its fills split. This ADR decides
when flatten sends another order, when it stops, and what it picks up after a restart. A throwaway
prototype drove these cases by hand, on branch
[`prototype/flatten-saga`](https://github.com/MarcosACH/tickwright/tree/prototype/flatten-saga).
Decided in [#413](https://github.com/MarcosACH/tickwright/issues/413), part of
[#408](https://github.com/MarcosACH/tickwright/issues/408).

## Decision

- **Each attempt is one reduce-only market order.** A market order is IOC, so it ends at once. It
  fills, fills partly, or fills nothing (P6 in the testnet probes,
  [#416](https://github.com/MarcosACH/tickwright/issues/416)). Flatten never leaves its own order
  resting. An attempt is sized to the venue position when it is placed. If the position shrank
  since, Hyperliquid shrinks the order to it (P1). What paper does is decided in
  [#414](https://github.com/MarcosACH/tickwright/issues/414).
  **(Resolved by ADR-0057:** paper shrinks the order to the account net too, and ends the cut part
  as `CANCELLED`. Decided in [#414](https://github.com/MarcosACH/tickwright/issues/414).**)**
- **One attempt at a time per symbol.** When an attempt ends, flatten reconciles, then decides
  again. If the venue still holds a position, it places the next seq for what is left.
- **Foreign flow is covered by the next attempt.** A hand trade that grows the position is healed
  into the unattributed partition, and the next attempt closes it. A hand trade that flips the
  position makes the next attempt go the other way.
- **A venue that goes flat ends flatten.** If a hand trade closes the venue during flatten, the
  books cannot see that trade. The leftover rule (ADR-0054) moves each strategy's size into the
  unattributed partition. It keeps the gap as a visible size finding. The account is flat, so
  flatten is done.
- **Flatten gives up after 3 attempts in a row that fill nothing.** Any fill resets the count. When
  it gives up, the kill switch stays tripped and the run exits with an error.
  **(Extended in [#431](https://github.com/MarcosACH/tickwright/issues/431):** a flatten order
  is priced like any market order. On Hyperliquid that is the last trade × (1 ± `SLIPPAGE_BOUND`),
  5% by default (ADR-0030). It is the last trade, not the mark. Paper still fills at the last tick.
  Flatten has no price rule of its own, so the adapter keeps the one bound. After an attempt that
  fills nothing, flatten waits 2 seconds on the engine clock before the next one. The wait is a
  constant, not a setting. After an attempt with any fill, the next one goes at once. A retry
  helps only if the price moves, so the exit run needs a price that updates between attempts.
  That is still open in #408, with the feed during an exit run.**)**
  **(Resolved by ADR-0059:** flatten starts the feed, so the price updates between attempts. A
  missing price counts as an attempt that fills nothing. The price is now also capped by the mark.
  A buy never goes above mark × (1 + `SLIPPAGE_BOUND`), and a sell never goes below
  mark × (1 − `SLIPPAGE_BOUND`). This holds for every market order. Decided in
  [#438](https://github.com/MarcosACH/tickwright/issues/438).**)**
  **(Extended in [#439](https://github.com/MarcosACH/tickwright/issues/439):** a venue rate limit
  is not a dry attempt. The venue may refuse an order because the address is over its limit. It
  may also answer HTTP 429 for the IP limit. Either way, the count does not move, and flatten waits
  10 seconds before it tries again. Each refusal emits a named event. A thin book and a rate limit
  mean different things. A thin book may never fill, so the count stops the run. A rate limit
  always lets one request through every 10 seconds, and each fill raises the address budget. So
  waiting still makes progress. Paper has no rate limit, so this never happens on paper.**)**
- **The count lives in memory.** A crash resets it. A new run is a new choice by the operator, so it
  gets its full tries.
- **On boot, an open flatten saga is resumed first.** The engine finds it at the venue by its id. It
  books every fill the venue reports, including fills that came while the engine was down. They
  split against the sizes stored with the saga. If the venue never saw the order, the saga ends as
  `failed` and books nothing.
- **Then the run starts its job from the top.** It trips the kill switch, which is already tripped.
  It runs cancel all, reconciles, and places the next seq. This matches the engine's boot order,
  where open sagas are resolved before positions are reconciled.
- **The split never takes a lot back.** It depends on the cumulative fill only, so a resume after a
  crash books the same moves. First the fill splits by side. Say the venue is long. Long partitions
  hold L lots, and short ones hold S lots. After f lots fill, the long side has moved
  `floor(L * f / (L - S))` lots in total. The short side has moved that, minus f. Then each side
  hands out its total one lot at a time. Each lot goes to the partition furthest behind its exact
  share at that point. A partition's exact share is its size times the lots handed out so far,
  divided by the side's size, L or S. Behind is measured in lots. It is that share minus the lots
  the partition already got. Ties go to the partition id that sorts first.
- **So the split books the fill exactly.** A later fill only moves a partition further toward zero.
  The moves sum to the fill after every fill, even when an attempt ends partly filled. A full fill
  moves every partition to exactly zero. Otherwise a rounding lot would open a gap between the books
  and the venue. The next attempt could not be placed until reconcile healed it.

Example. A holds BTC +0.6, B holds +0.6, and C holds +0.2. The lot is 0.1. After 1.0 fills, the
moves are A -0.4, B -0.4, and C -0.2. After 1.1 fills, they are A -0.5, B -0.4, and C -0.2. The
second fill moves only A.

## Considered options

- **A resting limit order.** It can rest while the price moves away. Flatten would then have to
  cancel it on a stop and on a crash. A market order never rests.
- **Retry forever.** On a thin book, the run would never end, and the operator could not tell it
  from progress.
- **Stop on the first attempt with no fill.** One miss can be a race. In the probes, the best bid
  moved between the book read and the order (P6).
- **Store the count of dry attempts.** A crash would carry a half-used budget into the operator's
  next run. That run is a new decision, so it starts at zero.
- **Let reconcile book a fill missed in a crash.** Reconcile heals a gap into the unattributed
  partition only. The strategies would not read flat.
- **Largest remainder on the cumulative fill.** The prototype used it. Each share rounds down to the
  lot, and the leftover lots go to the largest remainders. A larger fill can then take a lot back.
  In the example above, C moves -0.2 after 1.0 fills, then -0.1 after 1.1 fills. The second fill
  would book C buying on a sell order.

## Consequences

- After reconcile, the books match the venue, unless the venue is flat. Then the leftover rule
  applies. So flatten never waits for the books to match. A venue read that fails is not covered
  here. It belongs with what the operator sees when flatten cannot finish, still open in #408.
- The exit code and the named events of a run that gives up are still open in #408.
- How far a flatten order's price may be from the mark, and how long to wait between attempts, are
  still open in #408. Both decide how often an attempt fills nothing.
  **(Resolved in [#431](https://github.com/MarcosACH/tickwright/issues/431):** the normal market
  order bound, and a 2 second wait after a dry attempt. See the block under the give-up rule
  above.**)**
- Flatten across many symbols is still open in #408.
  **(Resolved in [#439](https://github.com/MarcosACH/tickwright/issues/439):** every symbol closes
  at the same time. Each symbol runs its own loop from this ADR, one attempt at a time. Each order
  goes out as its own action, never in a batch. The address limit counts a batch of n orders as n
  requests, so a batch saves nothing there. It would only save IP weight, and it would need a new
  place-a-list seam method. One after another would leave the last symbol open longest. ADR-0054
  already sends one order per symbol, and ADR-0055 batches cancels only. Neither changes. The
  limit of 1000 open orders does not reach flatten, because cancel all runs first.**)**
- Each attempt takes a new seq. A flatten of one symbol can use several `__operator__` seqs.
