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
  | 2 | Refused before it touched the venue. |

- **Done is what the venue says at the end.** Cancel all is done when the venue holds no resting
  order. Flatten is done when it holds no resting order and no position.
- **Code 1 covers every run that started and did not finish.** That is flatten giving up, a failed
  read, cancel all leaving orders, a fault, and Ctrl-C. Under ADR-0024, Ctrl-C gives 0. Here it
  gives 1, because the job is not done.
- **Code 2 is the store check.** The store lock is held, or the store is new and `--new-store` was
  not passed (ADR-0052). The run has not booted, so nothing at the venue moved.
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
  read counts toward the 3-in-a-row stop, like a missing price (ADR-0059). Flatten retries after
  the 2 second wait. After 3 in a row it exits 1. The kill switch stays tripped. The open saga stays
  in the store, and the next run resumes it on boot (ADR-0056).
- **Named events.** The `order.*` events already cover each order. An exit run adds these:

  | Event | When | Fields |
  | --- | --- | --- |
  | `exit.refused` | The run exits 2 | `reason`: `lock_held` or `new_store` |
  | `exit.unguarded` | Flatten runs under `GUARD=noop` (ADR-0053) | the restart warning |
  | `cancel_all.orders_remain` | Orders survive the second cancel | their oids |
  | `flatten.dry_attempt` | An attempt counts toward the 3 | `symbol`, `count`, `reason` |
  | `flatten.rate_limited` | The venue refuses for a rate limit (ADR-0056) | `symbol`, `limit` |
  | `flatten.gave_up` | 3 dry attempts in a row | `symbol`, the size left |
  | `exit.finished` | Every run that booted | `job`, `outcome`, `exit_code`, what is left |

  `reason` is `no_fill`, `no_price`, `read_failed`, or `refused`. `refused` covers every refusal
  that is not a rate limit, and a send that dies (ADR-0056). `limit` is `address` or `ip`. What is
  left comes from the last venue read. If that read failed, the field says so.
- **No separate summary.** `exit.finished` is the summary. It goes through the same logger as every
  other event, so it is the last line the operator sees.

## Considered options

- **Two codes, 0 and 1.** A script could not tell a run that touched nothing from one that stopped
  halfway. After a refusal the operator fixes the lock or the path. After a stop they check the
  account.
- **Trust the per-order cancel statuses.** A filled order and a made-up id give the same error.
  A child cancelled with its parent gives an error too. The statuses cannot say what still rests.
- **Retry a failed read with its own budget.** It is a second count beside the 3 attempts. The 3
  attempts already end a run that cannot make progress.
- **Wait for a failed read with no limit.** A dead connection would hang the run.
- **Print a summary table to stdout.** It is a second output to keep in step with the events.

## Consequences

- A supervisor must not wrap an exit run. Its code 1 means "look at the account", not "restart".
- A short venue outage is ridden out. One that lasts past 3 tries ends the run, and the operator
  runs flatten again.
- Not built yet. The build adds the seven events to the catalog.
