# Module Map: Strategy wake-ups

## Source

PRD: [#553: Let a strategy wake on a clock schedule, even when ticks stop](https://github.com/MarcosACH/tickwright/issues/553).
Decisions: [ADR-0062](../adr/0062-strategies-wake-on-a-clock-aligned-interval.md). Timing reuses
`Clock.sleep_until` (ADR-0033). The duplicate rule extends the tick gate of ADR-0025. Terms are
used exactly as defined in `CONTEXT.md`, where **Wake** is the new one.

Additions to the [v1 core-engine map](./v1-core-engine.md), which stands unchanged.

## Modules

### Wake (`domain/events.py`, new event)

**Interface:** A frozen event for one strategy. It carries `strategy_id`. Its `ts_event` is the
scheduled slot, not the time it was built. `partition_key` is the `strategy_id`, so one strategy's
wake-ups stay in order on Kafka. `event_id` is derived from the `strategy_id` and the slot.

**Responsibilities:** Be the one fact "this strategy's slot T has come". Nothing else.

**Seams:** None. It is a leaf of the `Event` tree, so the Kafka codec and every walk over
`publishable_event_types()` cover it with no registration.

**Depth note:** A value type. It earns its place by being the thing both buses can order.

---

### Strategy (`domain/protocols.py`, existing seam)

**Interface:** Two required members join it. `wake_interval_seconds: float | None` is the interval,
and `None` means the strategy never wakes. `async def on_wake(self, wake: Wake) -> None` handles a
wake-up. A strategy that never wakes writes an empty one.

**Responsibilities:** Unchanged otherwise. A strategy keeps any deadline in its own snapshot and
checks it on each wake-up.

**Seams:** The existing one. User strategies and the reference strategies are its adapters.

**Depth note:** Both members are required, so mypy and the loader catch a missing one.

---

### Wake interval rule (`domain`, new)

**Interface:** Takes an interval and returns a refusal sentence, or `None` when the value is
fine. `None` is fine. A value under 1 second is refused, and so is anything that is not a number.

**Responsibilities:** Hold the one rule both gates apply, in one sentence they both print.

**Seams:** None.

**Depth note:** Same shape as `reserved_id_refusal`. Without it the loader and the host would
each write the rule, and could come to disagree.

---

### Aligned loop (`engine/cadence.py`, new, beside `run_cadence`)

**Interface:** Takes a `Clock`, an interval, and a cycle that receives the slot time. Runs until
cancelled. Slots are multiples of the interval since the epoch. A cycle runs once per slot it
reaches. Slots missed by downtime or a replay jump are skipped, not caught up.

**Responsibilities:** The grid math and the skip rule. It waits with `Clock.sleep_until`, so
`ManualClock` drives it in replay and in tests.

**Seams:** None. It knows nothing about `Wake` or strategies.

**Depth note:** All loops that pace on the clock live in one file. Without it, the grid math
lands in the runner.

---

### StrategyHost (`engine`, existing)

**Interface:** `register` also refuses a bad interval, as `InvariantViolation`, through the wake
interval rule. A new read hands the runner one pair per waking strategy: its interval and a
function that publishes that strategy's `Wake` for a given slot. Strategies with `None` are left
out.

**Responsibilities:**

- Keep the interval `register` checked. The loop and the drop rules use that copy, because
  `restore()` runs later and could change the strategy's own value.
- Build and publish each `Wake`.
- Subscribe to `Wake` in `start()`, after restore, like ticks.
- Deliver a `Wake` only to its own strategy.
- Drop a `Wake` whose slot is at or before the last one delivered to that strategy.
- Drop a `Wake` older than that strategy's interval, measured against the `Clock`.
- Run `on_wake` inside the same containment as `on_tick`, then save the snapshot.

**Seams:** None.

**Depth note:** Everything about a `Wake` lives here: its fields, its target, both drop rules.
The host still runs no tasks of its own.

---

### Runner (`engine`, existing)

**Interface:** Unchanged for callers.

**Responsibilities:** In `_schedule_cadences`, start one aligned loop per pair the host hands
over. The loops join the cadence tasks, so they stop with them in the reverse shutdown. An exit
run schedules no cadences, so it starts no loops.

**Seams:** None.

**Depth note:** The runner schedules and knows nothing about `Wake`. It already treats the
reconcile cycles the same way.

---

### Strategy loader (`app/build.py`, existing)

**Interface:** Unchanged for callers. A bad interval now refuses with `StrategyLoadRefusal`,
which exits 2.

**Responsibilities:** The method check already reads the protocol, so it refuses a strategy
without `on_wake` with no change. After construction, it reads `wake_interval_seconds`. A missing
attribute refuses, as a missing `strategy_id` does. A bad value refuses through the wake interval
rule.

**Seams:** None.

**Depth note:** Gate 1 of 2. The exit code tells the operator that a restart will not help.

---

### Reference strategies and the example (`strategies`, `examples`, existing)

**Interface:** `SingleShotLimitStrategy` replaces the `cancel_after_ticks` setting with
`cancel_after_seconds`. The others gain the two members, set to `None` and an empty handler.

**Responsibilities:** The limit strategy wakes every 1 second, fixed in the class. Its deadline
is the placing tick's `ts_event` plus `cancel_after_seconds`, saved in the snapshot. It cancels
once a wake-up's `ts_event` reaches the deadline. A strategy has no `Clock`, so it reads time from
event timestamps.

**Seams:** These are adapters of `Strategy`.

**Depth note:** The engine uses its own feature, so `/verify-tickwright` can prove it on the real
process.

---

## Docs that move with the change

- `docs/extending.md`, where it describes the `Strategy` seam.
- The `Strategy` entry in `CONTEXT.md`.
- Every test fake that implements `Strategy` gains the two members in the same PR as the
  protocol change. A fake that never wakes gets both from `NeverWakes` in
  `tests/_support/never_wakes.py`. A fake that wakes overrides both. The strategies in
  `tests/_support/user_strategies.py` write both out, the way a user does.

## Dependency graph

```
app.build → domain.wake interval rule
app.build → engine.Engine
engine.Runner → engine.aligned loop → domain.Clock
engine.Runner → engine.StrategyHost
engine.StrategyHost → domain.Wake, domain.wake interval rule, domain.EventBus, domain.Clock
strategies → domain only
```

No cycles. The aligned loop does not import the host, and the host does not import the loop.
Direction is `app → engine → domain`, as `import-linter` enforces (ADR-0032).

## Out of scope

- **A `Wake`-specific loop inside the host.** The host would start running tasks.
- **The runner building the `Wake`.** It would split the `Wake` rules over two modules.
- **A host-only interval gate.** A bad interval would exit 1 and look like an engine fault.
- **An interval setting on the reference strategy.** A fixed 1 second shows the feature.
- **A flag on `run_cadence` for alignment.** Two small loops read more plainly than one switch.
