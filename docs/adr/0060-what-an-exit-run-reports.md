# What an exit run reports

An exit run is `tickwright cancel-all` or `tickwright flatten`, run by hand with the engine stopped
(ADR-0052). The operator needs to know if the job is done. A script needs the same answer from the
exit code alone. This ADR decides the exit codes, how cancel all knows it is done, what flatten does
when a venue read fails, and which named events an exit run emits. Decided in
[#441](https://github.com/MarcosACH/tickwright/issues/441), part of
[#408](https://github.com/MarcosACH/tickwright/issues/408).

## Decision

- **Three exit codes.** They replace the ADR-0024 contract for an exit run only. The engine keeps
  its own.

  | Code | Meaning |
  | --- | --- |
  | 0 | Done. A final venue read confirms it. |
  | 1 | Stopped partway. The account may still hold orders or positions. |
  | 2 | Never booted. Nothing at the venue moved. |

- **Done is what the venue says at the end.** Cancel all is done when the venue holds no resting
  order. Flatten is done when it holds no resting order and no position.
- **Code 1 covers every run that started and did not finish.** That is flatten giving up, a failed
  read, cancel all leaving orders, a fault, and a stop by signal. Under ADR-0024, SIGINT (Ctrl-C)
  and SIGTERM give 0. Here both give 1, because the job is not done.
- **Code 2 means the run never booted.** The store check refused it: the store lock is held, or the
  store is new and `--new-store` was not passed (ADR-0052). Or the config or the command line is
  invalid. Nothing at the venue moved. Only the store check emits `exit.refused`. A config or
  command line error prints its message and emits no event, because logging needs a valid config.

  **(Extended by [#468](https://github.com/MarcosACH/tickwright/issues/468):** a store the run
  cannot open or lock is code 2 too. It fails before boot, so nothing at the venue moved. Code 1
  would tell a script to check the account for no reason. A store that fails while the run is being
  built prints its message and emits no event, like a config error. A store whose lock read fails
  emits `engine.faulted` naming the error. Neither emits `exit.refused`, because the store check
  never answered.**)**
- **Cancel all trusts the read, not the statuses.** Hyperliquid answers one status per cancel
  (P4 in the probes). "Already filled", "already cancelled", and "never placed" share one error
  string. Cancelling a parent also cancels its TP and SL, so they answer "already canceled" (P5).
  So a "gone" status is not a failure and emits no warning. After the cancels, the run reads open
  orders again. Empty means done.
- **Cancel all tries once more, then stops.** If orders remain, it cancels them again and reads
  again. If any still remain, it emits `cancel_all.orders_remain` and exits 1. Flatten stops there
  too. It never closes positions while an order rests, because that order could fill and reopen
  exposure.
- **A failed venue read is a dry attempt.** Flatten reads the order by cloid for an attempt's
  verdict. It reads positions to size the next attempt. A failed read proves nothing (ADR-0011
  inv 1). So the saga stays open, and flatten never sends an order it could not size. The failed
  read counts toward the 3-in-a-row stop, like a missing price (ADR-0059). After the 2 second
  wait, flatten retries the read that failed. If the cloid read failed, it reads the same saga
  again. It sends no new seq until that saga has a verdict, so a symbol never has two attempts
  open (ADR-0056). Each failed read counts once. An attempt that then ends with no fill counts once
  more. So a refused attempt whose first cloid read fails counts twice. After 3 in a row it exits 1.
  The kill switch stays tripped. The open saga stays in the store, and the next run resumes it on
  boot (ADR-0056).
- **A read refused for a rate limit is not a failed read.** The IP limit covers reads too. Only
  the address limit skips them (ADR-0044). A rate-limited read takes the ADR-0056 rate limit path.
  The count does not move. Flatten emits `flatten.rate_limited` with `limit` as `ip`. It waits 10
  seconds, then reads again. Waiting makes progress here, so the wait has no cap, like an order's.
  **(Extended by the [safe exit module map](../module-maps/safe-exit.md),
  [#457](https://github.com/MarcosACH/tickwright/issues/457):** a read reports this as
  `VenueReadFailure.RATE_LIMITED_IP`. So `fetch_open_orders()` and `fetch_positions()` return a
  `VenueReadFailure` rather than `None`. With `None`, flatten could not tell a rate-limited read
  from a failed one. ADR-0056 holds the rest of the refusal report.**)**
  **(Narrowed in [#475](https://github.com/MarcosACH/tickwright/issues/475):** only the reads
  inside a symbol's close loop wait out the limit. Those are the position read and the read by
  cloid. A reconcile pass now returns `PassResult`, so flatten can tell a rate-limited pass from a
  failed one. The first position read and the final read still stop the run on any failed read,
  a 429 included. No order goes out after either one, so stopping there is safe.**)**
- **Named events.** The `order.*` events already cover each order. An exit run adds these:

  | Event | When | Fields |
  | --- | --- | --- |
  | `exit.refused` | The store check refuses the run | `reason`, `store`, the lock holder |
  | `exit.new_store` | A run goes ahead with `--new-store` (ADR-0052) | `store`, the warning |
  | `exit.unguarded` | Flatten runs under `GUARD=noop` (ADR-0053) | the restart warning |
  | `cancel_all.orders_remain` | Orders survive the second cancel | their oids |
  | `flatten.dry_attempt` | An attempt counts toward the 3 | `symbol`, `count`, `reason` |
  | `flatten.rate_limited` | The venue refuses for a rate limit (ADR-0056) | `symbol`, `limit` |
  | `flatten.gave_up` | 3 dry attempts in a row | `symbol`, the size left |
  | `exit.finished` | Every run that booted | `job`, `outcome`, `exit_code`, what is left |

  On `exit.refused`, `reason` is `lock_held` or `new_store`. `store` is the SQLite file's absolute
  path, or the Postgres host and database with no credentials. The lock holder is what ADR-0052
  prints for a held lock: the pid on SQLite, and the pid, client address, and start time on
  Postgres. On `flatten.dry_attempt`, `reason` is `no_fill`, `no_price`, `read_failed`, or
  `refused`. `refused` covers every refusal that is not a rate limit, and a send that dies
  (ADR-0056). `limit` is `address` or `ip`. What is left comes from the last venue read. If that
  read failed, the field says so.

  **(Field names as shipped, since [#468](https://github.com/MarcosACH/tickwright/issues/468):**
  the lock holder is the field `holder`. `cancel_all.orders_remain` names its orders in one field,
  `orders`, as a comma-separated list. An order with no oid is named by its cloid. What is left on
  `exit.finished` is the field `left`. It is `none` when the last read found no order. It is
  `read_failed: <reason>` when that read failed. It is `unread` when the job never returned, as
  after a signal or a fault. Each is one string because every catalog field is a scalar
  (ADR-0020).**)**

  **(Extended by [#472](https://github.com/MarcosACH/tickwright/issues/472):** each job writes
  its own `left`, because only the job knows what its last read covered. The engine writes only
  `unread`. Flatten's `left` lists the resting orders first, then each position as `SYMBOL=size`
  with the signed size, in one comma list. For example `8812,BTC=0.5`.**)**

  **(Field names as shipped, since [#473](https://github.com/MarcosACH/tickwright/issues/473):**
  `size` on `flatten.gave_up` is the last size the venue reported for the symbol, signed, as a
  string. After failed reads it can be from before the last close, so it can be more than is left.
  `exit.finished` says what is left. `count` on
  `flatten.dry_attempt` is the dry attempts in a row so far, from 1 to 3.**)**

  **(Extended by [#473](https://github.com/MarcosACH/tickwright/issues/473):** `reason` on
  `flatten.dry_attempt` can also be `size_gap`. That is a gap between the partitions and the venue
  size that a drain did not close (ADR-0054). No close goes out against it.**)**
- **No separate summary.** `exit.finished` is the summary. It goes through the same logger as every
  other event, so it is the last line the operator sees.

## Considered options

- **Two codes, 0 and 1.** A script could not tell a run that touched nothing from one that stopped
  halfway. After a refusal the operator fixes the lock, the path, or the config. After a stop they
  check the account.
- **Trust the per-order cancel statuses.** A filled order and a made-up id give the same error.
  A child cancelled with its parent gives an error too. The statuses cannot say what still rests.
- **Retry a failed read with its own budget.** It is a second count beside the 3 attempts. The 3
  attempts already end a run that cannot make progress.
- **Wait for a failed read with no limit.** A dead connection would hang the run.
- **Count a rate-limited read like any failed read.** Under the IP limit, three refused reads would
  end the run in about 6 seconds. ADR-0056 already decided that a rate limit is waited out.
- **Print a summary table to stdout.** It is a second output to keep in step with the events.

## Consequences

- A supervisor must not wrap an exit run. Its code 1 means "look at the account", not "restart".
- A short venue outage is ridden out. One that lasts past 3 tries ends the run, and the operator
  runs flatten again.
- Not built yet. The build adds the eight events to the catalog.

  **(Partly built, since [#468](https://github.com/MarcosACH/tickwright/issues/468):**
  `tickwright cancel-all` runs on paper. The catalog has `exit.refused`, `exit.finished`, and
  `cancel_all.orders_remain`. `exit.new_store` comes with #469. `exit.unguarded` and the three
  `flatten.*` events come with the flatten slices.**)**

  **(Partly built, since [#472](https://github.com/MarcosACH/tickwright/issues/472):**
  `tickwright flatten` runs on paper with the replay feed. Any other exchange or feed exits 2
  before boot, until #475. The catalog has `exit.unguarded`.**)**

  **(Partly built, since [#470](https://github.com/MarcosACH/tickwright/issues/470):**
  `tickwright cancel-all` also runs on Hyperliquid. It cancels a hand-placed order by its oid.**)**

  **(Partly built, since [#475](https://github.com/MarcosACH/tickwright/issues/475):**
  `tickwright flatten` also runs on Hyperliquid with the live feed. It closes a coin held by hand
  that the config never named. Only a live exchange with the replay feed still exits 2 before
  boot. A replay row is not the market.**)**
