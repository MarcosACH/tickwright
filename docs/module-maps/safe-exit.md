# Module Map: Safe exit on real money

## Source

PRD: [#457: Exit orders and positions safely on real money](https://github.com/MarcosACH/tickwright/issues/457).
Decisions: the wayfinding map [#408](https://github.com/MarcosACH/tickwright/issues/408) and ADRs
0052 to 0060. Terms are used exactly as defined in `CONTEXT.md`.

Additions to the [v1 core-engine map](./v1-core-engine.md) and the
[pre-trade limits map](./pre-trade-limits.md), which stand unchanged:

```
src/tickwright/
  domain/
    events.py                 # + CancelAllSignal, reduce_only, OrderRefusedReport
    venue.py                  # + VenueOpenOrder, ExternalOrderRef, RATE_LIMITED_IP
    order.py                  # + split_basis, the operator cancel mark, request_cancel by seq
    position.py               # + OPERATOR, beside UNATTRIBUTED
    instrument.py             # + the strategy's own position on PreTradeReading
    protocols.py              # Exchange: cancel(refs), fetch_open_orders, fetch_positions
                              # MarketFeed: add_symbols. Store: lock, seq record
  engine/
    exit_job.py               # ExitJob, ExitContext, FeedHandle, ExitOutcome,  NEW
                              # OperatorCancelAll
    flatten.py                # Flatten and its private per-symbol loop        NEW
    flatten_split.py          # the pure pro rata split                        NEW
    runner.py                 # Engine takes an optional ExitJob, no cadences on it
    execution.py              # CancelAllSignal, __operator__ orders
    checkpoint.py             # the split on the fill write, own position read
    guard.py                  # the ADR-0058 table
    strategy_host.py          # the high-water reads the seq record
    reconcile.py              # RATE_LIMITED_IP stops a pass
  adapters/
    paper/exchange.py         # the reduce-only rules, the two new reads
    store/                    # the lock, the seq record, split_basis
    feed/replay.py            # add_symbols is a no-op
  venues/hyperliquid/         # r flag, batches of 200, refusals, mark cap, reads, feed symbols
  strategies/emitter.py       # cancel_all(), reduce_only on place()
  observability/catalog.py    # the 8 new events
  app/
    __main__.py               # cancel-all and flatten commands, --new-store
    build.py                  # build the exit job, the feed-ends flag
    config.py                 # refuse __operator__ as a strategy id
```

## Modules

### Engine (`engine`, existing)

**Interface:** Gains one optional constructor argument, an `ExitJob`. With no job, nothing changes
for callers. With a job, `run()` returns 0, 1, or 2 (ADR-0060) instead of the ADR-0024 codes.

**Responsibilities:**

- Take the store lock first, before `recover()` writes anything. On a normal run, a held lock is a
  fault: exit 1, with `engine.faulted` naming the holder. On an exit run it is exit 2 with
  `exit.refused`.
- On an exit run, call `job.admit(store)` after the lock and before `recover()`. A refusal is
  exit 2.
- Boot as today: recover, bus, exchange, barrier. The barrier's startup reconcile resolves any open
  flatten saga by cloid. Its fills split by their stored `split_basis`. So "resume first" is the
  boot itself (ADR-0056).
- On an exit run, skip `host.start()`. Build an `ExitContext` from the parts the engine already
  owns. Then open the `TaskGroup` as today, with three changes:
  - No reconcile cadences. The job runs every pass itself, so two passes never overlap.
  - No feed task at the start. The job starts the feed through the `FeedHandle` in its context.
    The handle runs `feed.run()` as a task in this group, so a feed fault still faults the run.
  - The job runs as a task in this group. `exchange.run()` runs beside it as today.
- When the job returns, request the stop and run the reverse shutdown as today.
- Map the job's `ExitOutcome`, a signal, or a fault to the exit code. SIGINT and SIGTERM cancel
  the job and give 1. A fault in any task of the group gives 1. Emit `exit.finished` as the last
  event of every exit run that booted.

**Seams:** None new.

**Depth note:** One host keeps one boot path. A second host would copy the wiring, and two boot
paths could drift on recovery. The engine builds its internals itself, so it hands them to the job
at run time. The composition root never sees them.

---

### ExitJob (`engine`, new Protocol)

**Interface:** `admit(store)` returns a refusal or nothing. It refuses a store with no account row
unless `--new-store` was passed (ADR-0052). With the flag, it emits `exit.new_store`.
`run(context)` returns an `ExitOutcome`: the job name, done or stopped, and what the last venue
read left.

`ExitContext` is a frozen value the `Engine` builds after the barrier. It holds the order and
account anchors, the guard, the `Checkpointer`, the `Reconciler`, the `LedgerReconciliation`, the
bus, the clock, and a `FeedHandle`. The handle has `start(symbols)`, which calls
`feed.add_symbols()` and `feed.start()` and then supervises `feed.run()`. It also has `ended()`,
which waits for `feed.run()` to return.

**Responsibilities:** The contract between the host and one operator job. A job is built by the
composition root with only the facts the root owns: the `--new-store` flag, and for `Flatten`
whether the feed ends. Everything the engine owns arrives in the context.

**Seams:** Two real implementations, `OperatorCancelAll` and `Flatten`.

**Depth note:** The host stays free of exit logic. Each job keeps its own retries and reads.

---

### OperatorCancelAll (`engine`, new)

**Interface:** An `ExitJob`. Built with the `--new-store` flag. Uses the anchors and the
`Checkpointer` from its context. Never starts the feed. Leaves the kill switch alone (ADR-0053).

**Responsibilities:**

- Read every resting order in the account with `fetch_open_orders()`.
- For an order with a saga, set the operator cancel mark and checkpoint it before the send.
  Without the marker, reconcile would judge it a ghost and mark it `REJECTED`.
- The operator mark sets `cancel_requested` even when it is already set. It leaves
  `cancel_signal_id` as it was and consumes no seq. So the seq fold is unchanged, and a later
  strategy cancel all still resends. `request_cancel` is not used here, because it skips an order
  that is already marked.
- Send a cancel for every order the read shows resting, marked or not. An earlier cancel may never
  have reached the venue.
- Send an external order as an `ExternalOrderRef`, by oid. It has no saga (the ADR-0011 exception
  in ADR-0052).
- Send every cancel through one `cancel(refs)` call.
- Read again. Trust the read, not the cancel statuses. If orders remain, cancel once more and read
  again. If any still remain, emit `cancel_all.orders_remain` and stop (ADR-0060).

**Seams:** None.

**Depth note:** `Flatten` runs it as its first step. A second copy of the cancel rule would drift.

---

### Flatten (`engine`, new)

**Interface:** An `ExitJob`. Built with the `--new-store` flag and a flag that says whether the
feed ends. The composition root sets that flag. It is true for replay. Uses the rest from its
context. Starts the feed through the `FeedHandle`.

**Responsibilities:**

- Trip the kill switch first, even when there is nothing to close (ADR-0053). Under `NoopGuard`,
  emit `exit.unguarded` and go on.
- Run `OperatorCancelAll` with the same context. Stop if orders remain (ADR-0060).
- Read the held symbols with `fetch_positions()`. Pass them to `FeedHandle.start()`.
- If the feed ends, wait for `FeedHandle.ended()`, so replay fills at the last row (ADR-0059).
  Otherwise wait for each symbol's first price.
- Run one reconcile pass at a time, under one lock. A pass covers every symbol. A loop that needs
  a pass waits for the one in progress, then runs its own, so it never reads a pass that started
  before its attempt ended.
- Run one private per-symbol loop for each held symbol, all at the same time. Wait for all of them.
- Do the final venue read. Done means no resting order and no position.
- Keep the `__operator__` seq counter. Start it from the same saga high-water fold the
  `StrategyHost` uses. `engine` cannot import `SignalEmitter`.

**The per-symbol loop (private):**

- Before the first attempt, wait for the verdict on any open `__operator__` saga for the symbol
  that the boot left open. A symbol never has two attempts open.
- Place only when the partition sizes sum to the venue size from `fetch_positions()` (ADR-0054).
  If they differ, call `bus.drain()` first, so every fill already published is applied. If they
  still differ and the venue has account truth, run `LedgerReconciliation.reconcile_account()`.
  It heals the gap into the unattributed partition (ADR-0038). "Account truth" is the
  `declares_genesis` test the runner already uses for the account cadence. Then check again.
- On paper the gap is only fills the store has not applied yet. Paper has no account truth, so the
  drain alone closes it.
- A gap that is still there counts as a dry attempt. So a gap that never heals ends in
  `flatten.gave_up`, not in a loop.
- A flat venue moves each leftover into the unattributed partition instead.
- Place one reduce-only market IOC order, as a `PlaceSignal` from `__operator__` on the bus. Size it
  from `fetch_positions()`.
- After each attempt, run the in-flight pass, `Reconciler.reconcile_inflight()`, until the saga has
  a verdict. Wait the in-flight interval between passes with `clock.sleep`. No cadence runs in an
  exit run. After a replay ends, nothing moves the replay clock, so a cadence would never fire.
- Count dry attempts: no fill, no price, a failed read, or a refusal (ADR-0056, ADR-0060). A fill
  resets the count. Stop after 3 in a row with `flatten.gave_up`.
- On an `OrderRefusedReport` or a dead send, resolve the saga by cloid before the next attempt.
- Wait 2 seconds after a dry attempt and 10 seconds after a rate limit. Both run beside the cloid
  verdict. A rate limit does not count. Waits use `clock.sleep`.
- Emit `flatten.dry_attempt` and `flatten.rate_limited`.

**Seams:** None.

**Depth note:** Every retry, wait, and stop rule from ADR-0056 lives here and nowhere else. The
loop never books a fill. Booking stays on the fill path, so a fill after a crash books the same way.

---

### flatten_split (`engine`, new, pure)

**Interface:** A pure function. It takes the partition sizes, the lot size, and the cumulative
fill. It returns each partition's total move toward zero. It also splits a fee by the absolute
size each partition books.

**Responsibilities:** The ADR-0056 rule. Split by side, then hand out lots one at a time to the
partition furthest behind its exact share. Ties go to the partition id that sorts first. The moves
sum to the fill after every fill. A later fill never takes a lot back.

**Seams:** None.

**Depth note:** A small interface over a subtle rule. It knows nothing about orders, so property
tests reach it directly.

---

### Checkpointer (`engine`, existing)

**Interface:** `checkpoint_fill` is unchanged for callers. `pre_trade_reading` gains the strategy's
own position. It also gains a read of every partition's size in a symbol, for `split_basis`.

**Responsibilities:** When an order has a `split_basis`, the fill write calls `flatten_split` on
the cumulative fill. It books each partition's change since the last fill, in one
`checkpoint_ledger` transaction. The `Store` seam already takes a tuple of positions.

**Seams:** None new.

**Depth note:** Every fill passes through here: live, paper, and reconcile after a crash. The
split is safe only on this path.

---

### ExecutionManager (`engine`, existing)

**Interface:** Unchanged for callers. It now consumes `CancelAllSignal` too.

**Responsibilities:**

- A `CancelAllSignal` raises the strategy's seq record first. Then it marks and cancels the
  strategy's open orders on that symbol with a lower seq. It skips terminal orders, and orders whose
  marker already has this seq or a higher one. Every cancel goes out in one `cancel(refs)` call
  (ADR-0055).
- An `__operator__` place reads the partition sizes from the `Checkpointer`. It stores them as
  `split_basis` in the same `PENDING` write that comes before the send (ADR-0054).
- `reduce_only` flows from the signal to the order and to the `PlaceOrder`.

**Seams:** None new.

**Depth note:** One saga path for strategy and operator orders. Dedup, the guard, and recovery are
reused, not copied.

---

### RealGuard and PreTradeReading (`engine`, `domain`, existing)

**Interface:** `check(signal, reading)` is unchanged. The reading gains the strategy's own net size
in the symbol.

**Responsibilities:** The ADR-0058 table. A strategy reduce-only order skips the size, value, and
position caps. It keeps the rate cap and the kill switch. It keeps the min notional unless it
closes the whole account net. It is denied unless it shrinks the strategy's own worst case. A
flatten order is reduce-only from `__operator__`. It skips the kill switch and every cap. Any other
`__operator__` order meets the kill switch (ADR-0054).

**Seams:** No new seam. `NoopGuard` ignores the new field.

**Depth note:** All guard rules stay in one place, as in the pre-trade limits map.

---

### StrategyHost and SignalEmitter (`engine`, `strategies`, existing)

**Interface:** The emitter gains `cancel_all(symbol=...)`, which returns its `signal_id`. `place()`
gains `reduce_only`.

**Responsibilities:** The seq high-water fold also reads the seq record, so a restart never reuses
a handled cancel all's seq (ADR-0055).

**Seams:** None new.

**Depth note:** The seq stays owned by the emitter. The host only resumes it.

---

### Exchange (`domain`, existing seam)

**Interface:**

- `OrderAnchor.cancel(refs)` takes a list of `OrderRef | ExternalOrderRef`. A single cancel sends
  a list of one. `ExternalOrderRef` names an order the engine did not place: a symbol and a
  required oid. `OrderRef` keeps its required cloid, so `fetch_order` and every reconcile read stay
  keyed by cloid.
- `AccountAnchor.fetch_open_orders()` returns every resting order in the account as
  `VenueOpenOrder` values (symbol, oid, cloid if any), or a `VenueReadFailure`.
- `AccountAnchor.fetch_positions()` returns a signed size per symbol, or a `VenueReadFailure`.
- `VenueReadFailure` gains `RATE_LIMITED_IP`. Reads only meet the IP limit.
- A new raw report, `OrderRefusedReport`, with `refusal`: `rate_limited_address`,
  `rate_limited_ip`, or `refused`. It moves no saga. Reconcile still resolves the order by cloid.

**Responsibilities:** Both new reads answer for the account, so they sit on `AccountAnchor`. That
is the placement rule in `protocols.py`.

The slice that changes the seam also fixes two docstrings in `protocols.py`:

- `OrderAnchor` says a cancel takes the cloid. An `ExternalOrderRef` cancel takes an oid.
- `AccountAnchor` says it has one live-only caller. The two new reads add flatten, operator cancel
  all, and paper as callers.

**Seams:** Two real adapters, `PaperExchange` and `HyperliquidExchange`. A third-party adapter
breaks on `cancel(refs)` and the two new reads. That is allowed on `0.x`, and the release notes
say so.

**Depth note:** The engine reads no venue text. A refusal reaches it as a closed set of kinds.

---

### PaperExchange (`adapters/paper`, existing)

**Interface:** Unchanged constructor. It already gets `account_net`.

**Responsibilities:**

- The ADR-0057 table at placement: accept, shrink to the net, or reject. A shrunk order ends with
  `CANCELLED` for the cut part.
- After every fill, shrink or cancel its resting reduce-only orders in that symbol.
- Count its own fills the store has not applied yet.
- Skip the min notional for a reduce-only order that covers the whole net.
- `fetch_open_orders()` reads its own resting book.
- `fetch_positions()` answers the store net plus the unapplied fills. That is the number the
  reduce-only rules already use. `fetch_account_state()` still answers `None`.
- `cancel(refs)` cancels each ref in turn. It never emits `OrderRefusedReport`.

**Seams:** None new.

**Depth note:** Paper copies the venue, so a strategy can test its exit on paper first.

---

### HyperliquidExchange and HyperliquidFeed (`venues/hyperliquid`, existing)

**Interface:** Unchanged constructors.

**Responsibilities:**

- Send the `r` flag from `PlaceOrder.reduce_only`.
- End a shrunk order with `CANCELLED` for the cut part. The venue answers `filled` (ADR-0057).
- Split a cancel list into batches of at most 200. Split `cancel` and `cancelByCloid` by whether a
  ref has an oid (ADR-0055).
- Emit `OrderRefusedReport` on a refused action and on a send that dies. Tell the address limit and
  the IP limit (HTTP 429) apart from any other refusal (ADR-0056).
- Cap every market order price by the mark too (ADR-0059). A missing mark is a missing price.
- Answer `fetch_open_orders()` and `fetch_positions()`. Return `RATE_LIMITED_IP` on a 429.
- The feed's `add_symbols()` adds coins to the subscription before `start()`.

**Seams:** None new.

**Depth note:** All Hyperliquid knowledge stays in the adapter.

---

### MarketFeed (`domain`, existing seam)

**Interface:** Gains `add_symbols(symbols)`, called before `start()`.

**Responsibilities:** Let a live flatten price coins the operator never configured (ADR-0059).

**Seams:** Two real adapters. `HyperliquidFeed` subscribes to them. `ReplayFeed` does nothing,
because the file decides which symbols it plays.

**Depth note:** The feed-ends flag stays out of this seam. Only the composition root needs it.

---

### Store (`domain` seam, `adapters/store`, existing)

**Interface:**

- `lock()` returns nothing when it gets the lock. Otherwise it returns the holder: the pid on
  SQLite, or the pid, client address, and start time on Postgres. The lock ends at `close()` or
  when the process dies.
- A per-strategy seq record that only goes up, and a read of it (ADR-0055).
- `Order.split_basis` is stored with the order row.

**Responsibilities:**

- SQLite: an OS lock on `<db>.lock`, never on the database file. The pid goes into the file for the
  message only.
- Postgres: an advisory lock on the connection the store writes through. The connect step sets the
  keepalives from ADR-0052. A lost session fails the next write with `InvariantViolation`, which
  already faults the engine.

**Seams:** Two real adapters, SQLite and Postgres. The store contract tests cover both.

**Depth note:** On Postgres the lock must share the write connection. Only the adapter can make
that true.

---

### App: CLI, composition root, config (`app`, existing)

**Interface:** `tickwright cancel-all` and `tickwright flatten`, each with `--new-store`. A
`build_exit_job(config, ...)` beside `build_engine`, which takes the pure `AppConfig`.

**Responsibilities:**

- Build the job from the command, `--new-store`, and the feed-ends flag, and pass it to the
  `Engine`. Set the feed-ends flag from `config.feed`. The root passes no engine internals. The
  `Engine` hands those to the job in its `ExitContext`.
- Refuse `__operator__` as a strategy id at config time (ADR-0054).
- An invalid config or command line exits 2, prints its message, and emits no event (ADR-0060).

**Seams:** None new.

**Depth note:** Impl choice stays one `match` in the composition root.

---

### Event catalog (`observability`, existing)

**Interface:** Eight new names: `exit.refused`, `exit.new_store`, `exit.unguarded`,
`exit.finished`, `cancel_all.orders_remain`, `flatten.dry_attempt`, `flatten.rate_limited`,
`flatten.gave_up`. Fields as in ADR-0060.

**Responsibilities:** A closed set, so the catalog tests cover them.

**Seams:** None.

**Depth note:** Unchanged role.

## Dependency graph

```
app.__main__ → app.build → engine.Engine, engine.OperatorCancelAll, engine.Flatten
engine.Engine → engine.ExitJob, engine.ExitContext, domain.Store (lock), domain.MarketFeed
engine.ExitContext → engine.Checkpointer, engine.Reconciler, engine.LedgerReconciliation
engine.ExitContext → domain.OrderAnchor, domain.AccountAnchor, domain.PreTradeGuard
engine.ExitContext → domain.EventBus, domain.Clock, engine.FeedHandle
engine.Flatten → engine.OperatorCancelAll, engine.ExitContext
engine.OperatorCancelAll → engine.ExitContext
engine.ExecutionManager → engine.Checkpointer → engine.flatten_split
engine.RealGuard → domain.PreTradeReading
adapters.paper, venues.hyperliquid → domain.Exchange, domain.MarketFeed
adapters.store → domain.Store
```

No cycles. `Flatten` places through the bus, not through the `ExecutionManager`. Direction is
`app → engine → domain`, as `import-linter` enforces (ADR-0032).

## Out of scope

- **A separate `ExitRun` host.** It would copy the `Engine` wiring. Two boot paths could drift on
  recovery.
- **Engine properties that expose its internals to the root.** The root would wire parts the
  `Engine` owns. The `ExitContext` keeps them inside `engine`.
- **Reconcile cadences during an exit run.** They would overlap the passes flatten runs itself.
  Under replay they never fire after the file ends anyway.
- **An optional `OrderRef.cloid`.** Every read keyed by cloid would have to handle a ref without
  one. A ref with no cloid and no oid would type-check. `ExternalOrderRef` keeps the two apart.
- **The split inside the flatten job.** A fill booked by reconcile after a crash would skip it.
- **A separate read for external orders only.** Cancel all would need two reads and two paths.
- **Paper answering `fetch_account_state` with positions.** Its `None` means "no account truth",
  and paper has no equity or margin to fill in.
- **A paper branch in flatten that reads the store.** Paper and live would drift.
- **`place()` returning a refusal.** Flatten orders go through the `ExecutionManager`, so the
  return value would never reach the job.
- **A `finite` property on `MarketFeed`.** Only the composition root needs that fact.
- **A seam method that places a list of orders.** ADR-0056 rules out a batched flatten.
- **An operator exit for one strategy.** See the PRD's out of scope.
