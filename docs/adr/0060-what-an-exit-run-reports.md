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
