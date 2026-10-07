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
  since, the venue shrinks the order to it (P1).
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
- **The count lives in memory.** A crash resets it. A new run is a new choice by the operator, so it
  gets its full tries.
- **On boot, an open flatten saga is resumed first.** The engine finds it at the venue by its id. It
  books every fill the venue reports, including fills that came while the engine was down. They
  split against the sizes stored with the saga. If the venue never saw the order, the saga ends as
  `failed` and books nothing.
- **Then the run starts its job from the top.** It trips the kill switch, which is already tripped.
  It runs cancel all, reconciles, and places the next seq. This matches the engine's boot order,
  where open sagas are resolved before positions are reconciled.
- **The split books the fill exactly.** Each partition's share is rounded down to the lot size. The
  lots left over go one at a time to the partitions with the largest remainder. So the moves sum to
  the fill, even when an attempt ends partly filled. Otherwise a rounding lot would open a gap
  between the books and the venue, and the next attempt could not be placed until reconcile healed
  it.

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

## Consequences

- After reconcile, the books match the venue, unless the venue is flat. Then the leftover rule
  applies. So flatten never waits for the books to match. A venue read that fails is not covered
  here. It belongs with what the operator sees when flatten cannot finish, still open in #408.
- The exit code and the named events of a run that gives up are still open in #408.
- How far a flatten order's price may be from the mark, and how long to wait between attempts, are
  still open in #408. Both decide how often an attempt fills nothing.
- Flatten across many symbols is still open in #408.
- Each attempt takes a new seq. A flatten of one symbol can use several `__operator__` seqs.
