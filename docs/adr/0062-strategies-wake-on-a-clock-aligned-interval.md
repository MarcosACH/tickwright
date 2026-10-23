# Strategies wake on a clock-aligned interval

A strategy could act only when a tick or an order event arrived. A strategy that should act every
15 minutes had to count ticks. A busy market sends many ticks and a quiet one sends few, so the
count meant a different time each hour. If ticks stopped, the strategy stopped. This ADR lets a
strategy wake on a fixed interval of clock time. Decided in a `/grill-with-docs` session on
2026-10-22 for [#535](https://github.com/MarcosACH/tickwright/issues/535).

## Decision

- **The strategy declares one interval.** `Strategy` gains `wake_interval_seconds: float | None`.
  `None` means the strategy never wakes. The strategy owns the interval, not the operator. So the
  interval and the handler cannot get out of sync. An author can still read the value from
  `settings`.
- **The handler is required.** `Strategy` gains `async def on_wake(self, wake: Wake) -> None`. A
  strategy that never wakes writes an empty one. The loader reads its method list from the
  Protocol, so it already refuses a strategy without `on_wake`.
- **A wake-up is a bus event.** The engine publishes a `Wake` event for one strategy. It carries the
  `strategy_id`, and its `ts_event` is the scheduled time. Both buses deliver one event at a time.
  So `on_wake` never runs while `on_tick` is paused at an `await`.
- **Wake-ups fall on a grid aligned to the epoch.** The slots are the multiples of the interval since
  the Unix epoch, in UTC. A 15-minute interval fires at :00, :15, :30 and :45. The grid does not
  depend on when the engine started. So replay, live and a restart agree on the times. An interval
  that does not divide a day fires at times that do not line up with the hours. That is allowed.
- **Missed slots are skipped.** After downtime or a jump in replay time, the strategy gets one
  wake-up, not one per missed slot. A deadline the strategy cares about lives in its own snapshot.
  It checks the deadline on each wake-up.
- **Duplicates are dropped like ticks (ADR-0025).** The host drops a wake-up whose scheduled time is
  at or before the last one it delivered to that strategy. It also drops a wake-up older than the
  strategy's own interval, measured against the `Clock`. So a Kafka backlog after a restart causes
  no burst of wake-ups. This rule does not use the tick staleness limit, which is off by default.
  **(Amended by [#561](https://github.com/MarcosACH/tickwright/issues/561):** the host also drops a
  wake-up whose scheduled time is ahead of the `Clock`. This life only sends a wake-up once the
  clock has reached its slot. So a wake-up ahead of the clock comes from an earlier life. A replay
  clock reads 0 until the first tick. Without this rule, a Kafka backlog on a replay restart had a
  negative age and reached `on_wake`.**)**
- **The interval is checked at registration.** An interval under 1 second is refused. Each wake-up
  is a bus event, a Kafka record and maybe a snapshot write. Work that needs to run faster belongs
  on ticks.
- **A wake-up has no special lifecycle rules.** Wake-ups start after the strategy is restored. They
  still arrive with the kill switch on, and the guard denies any order they cause. An exit run has
  no strategies, so it sends no wake-ups. The wake-up loops stop with the reconcile cadences.

The timing uses `Clock.sleep_until` (ADR-0033). So `ManualClock` drives it in replay and in tests.
No new seam is needed.

## Considered options

- **Timers created at run time.** Rejected. A list of timers has to survive a
  restart. Either the engine saves it, or every author creates the timers again. One interval plus
  deadlines in the snapshot covers the same cases.
- **A direct call from a timer task.** Rejected. The call could run between two steps of `on_tick`.
  The strategy would then see its own state half updated.
- **An optional `WakingStrategy` protocol.** Rejected. It needs a new guard against a misspelled
  handler, and mypy cannot tell that a strategy meant to wake. Its only gain was to spare existing
  users a breaking change, and there are none.
- **The interval in operator config.** Rejected. An operator could leave it out. The strategy would
  then never wake, and nothing would warn anyone.
- **Count from start, like `run_cadence`.** Rejected. The times would shift on every restart and
  differ between replay and live. A switch between both modes was also rejected, because nobody
  needs the second mode.
- **Reuse the tick staleness limit.** Rejected. It is off by default, so a Kafka backlog would
  deliver every old wake-up in a burst.
- **Save the last delivered wake-up in the store.** Rejected for now. The gap that remains is a crash
  after a signal and before the snapshot save. Ticks have the same gap. Closing it is a separate
  issue that covers both.

## Consequences

- `Strategy` gains two required members, so every strategy must add them. On `0.x` that is a MINOR
  bump.
- `Wake` is a new event type in the Kafka record format.
- The reference `SingleShotLimitStrategy` cancels after `cancel_after_seconds` instead of
  `cancel_after_ticks`.
