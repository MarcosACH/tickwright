# Module Map: Pre-trade limits

## Source

PRD: [#377 — Stop a strategy bug from sending a huge order or an order flood](https://github.com/MarcosACH/tickwright/issues/377).
Decision: [ADR-0051](../adr/0051-pre-trade-limits.md), with amendments to ADR-0017 and ADR-0039.
Terms are used exactly as defined in `CONTEXT.md`.

Additions to the [v1 core-engine map](./v1-core-engine.md), which stands unchanged:

```
src/tickwright/
  domain/
    instrument.py             # + PreTradeReading, beside Approved/Denied  NEW
    protocols.py              # PreTradeGuard.check gains the reading
  engine/
    guard.py                  # + PreTradeLimits, and the caps on RealGuard
    checkpoint.py             # + the read that builds a PreTradeReading
    portfolio.py              # + a read of the latest mark and its time
    execution.py              # fetches the reading, passes it to check
  app/
    config.py                 # + top-level limits, and the noop refusal
```

## Modules

### PreTradeReading (`domain`)

**Interface:** A frozen value for one symbol and one side. It carries the [[Account net size]] of
the symbol, the unfilled remainder of every open order on that side, and the latest mark price with
its time. The mark part is absent when no mark was ever seen. It holds no clock and makes no
judgment.

**Responsibilities:** Carry what the guard needs to judge the caps, as one snapshot taken inside one
synchronous call. No `await` sits between building it and checking it, so the parts cannot disagree.

**Seams:** None. It is a value.

**Depth note:** Without it the guard would need handles to the `Cache` and the
`PortfolioProjection`, which do not exist when `build_engine` builds the guard. Or the
`ExecutionManager` would do the cap math. Either scatters the rule.

---

### PreTradeGuard (`domain`, existing seam)

**Interface:** `check(signal, reading) -> GuardDecision`. The rest is unchanged. A denial is
`DENIED`, never sent, safe to recreate. A signal for a symbol with no spec still raises
`InvariantViolation`.

**Responsibilities:** Unchanged. The Protocol only gains the reading parameter.

**Seams:** Two real adapters, `RealGuard` and `NoopGuard`. `NoopGuard` ignores the reading. A user
guard must add the parameter. That is a breaking change, allowed on `0.x`, and the release notes say
so.

**Depth note:** The seam already exists and passes the deletion test (ADR-0017).

---

### PreTradeLimits (`engine`, beside `RealGuard`)

**Interface:** Frozen settings. A per-symbol map of three optional caps: max order size in coins,
max order value in USD, max position in coins. An optional engine-wide rate cap of N placements per
S seconds. A mark max age, default 10 seconds. Construction refuses a non-positive cap, and a rate
cap with only one of N and S set. An empty value means no limits.

**Responsibilities:** Hold the caps and reject nonsense at boot. It holds no state and does no
checking of orders.

**Seams:** None. Only `RealGuard` reads it, like `ReconcileConfig` and `ValuationBand` beside their
own users.

**Depth note:** It gives `AppConfig` a typed target, and the `engine` cannot import `app`.

---

### RealGuard (`engine`, existing)

**Interface:** Built with its specs, store, clock, and a `PreTradeLimits`. Checks run in a fixed
order: kill switch, spec lookup, quantization, size rounds to zero, min notional, then the caps.
Min notional applies to limit orders only. Every cap applies to both limit and market orders, so a
market order no longer returns before the checks end. The rate cap is last, so an order denied by
another cap never takes a slot. Every cap denial carries a reason that names the cap.

**Responsibilities:**

- Max order size: the quantized quantity against the symbol's cap.
- Max order value: quantity times the quantized limit price, or times the reading's mark for a
  market order. A missing mark, or one older than the max age on the guard's clock, denies the
  market order.
- Max position: the worst-case net size on the order's side, from the reading plus the new order.
  An order that moves this worst case toward zero passes, even when the result is still above the
  cap. A sell that shrinks a long position can still be denied when open sells push the worst case
  past the cap. A flip is judged on the new side.
- Rate cap: a sliding window of approved placement times, in memory, empty at boot.

**Seams:** None new.

**Depth note:** All cap rules live here and nowhere else. The check is a pure function of the
signal, the reading, the clock, and the window, so tests build a reading by hand.

---

### Checkpointer (`engine`, existing)

**Interface:** Gains one read: the `PreTradeReading` for a symbol and side. It reads the `Cache` and
the `PortfolioProjection` it already owns. It writes nothing.

**Responsibilities:** Define "account net size, same-side open remainder, and mark" once, next to
the data.

**Seams:** None new.

**Depth note:** It already owns both read-models (ADR-0043 §4). A second place that folds them would
be a second definition to drift.

---

### PortfolioProjection (`engine`, existing)

**Interface:** Gains one read: the latest mark price and its time for a symbol, or nothing when no
mark was ever seen.

**Responsibilities:** Lend out the private mark map for the `PreTradeReading`. The read path still
never checks the mark's age. The guard does that.

**Seams:** None new.

**Depth note:** The mark map lives here (ADR-0039). The `Checkpointer` is its second reader. See the
ADR-0039 amendment.

---

### ExecutionManager (`engine`, existing)

**Interface:** Unchanged for callers.

**Responsibilities:** On a placement, after the re-seen `signal_id` dedup, fetch the reading from the
`Checkpointer` and pass it to `check`. Cancels skip the guard, as today. It holds no cap logic.

**Seams:** None new.

**Depth note:** A pass-through for this feature by design.

---

### AppConfig (`app`, existing)

**Interface:** Gains a top-level `limits` field, `TICKWRIGHT_LIMITS__*`, next to `guard`. Validation
refuses any limit together with `guard = "noop"`, with a clear error.

**Responsibilities:** Map settings onto `PreTradeLimits`, and refuse a limit that would not be
enforced. `build_guard` hands the limits to `RealGuard`.

**Seams:** None new.

**Depth note:** The refusal lives on the pure `AppConfig`, so tests reach it without ambient config.

## Dependency graph

```
app.AppConfig → engine.PreTradeLimits
app.build_guard → engine.RealGuard
engine.ExecutionManager → engine.Checkpointer → engine.Cache, engine.PortfolioProjection
engine.ExecutionManager → domain.PreTradeGuard → domain.PreTradeReading
engine.RealGuard → engine.PreTradeLimits, domain.PreTradeReading, domain.Clock
```

No cycles. Direction is `app → engine → domain`, as `import-linter` enforces (ADR-0032).

## Out of scope

- **A separate rate-window class.** A few lines. The class would be almost as big as its code.
- **A guard that holds handles to engine state.** It would need two-step construction.
- **Building the guard inside `Engine`.** It moves guard wiring out of the composition root
  (ADR-0031).
- **A read Protocol over the `Checkpointer`.** One implementation, and a value does the job.
- **`PreTradeLimits` in `domain`.** It never crosses a seam.
