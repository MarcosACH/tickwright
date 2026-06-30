# Component lifecycle + error model: 4-state, crash-only, two error classes

Every long-lived component (`MarketFeed`, `Strategy`, `Exchange`, the `Engine` host) shares one
lifecycle, a readable component-state FSM:

- Methods: `async start()`, `async stop()`, a read-only `state` property, `health()`.
- States: **`READY → RUNNING → STOPPED`**, plus **`FAULTED`** (unrecoverable). `DEGRADED`,
  `DISPOSED`, and the transitional states are **deferred**.
- Override hooks: `on_start()` / `on_stop()` (subscribe / clean up). No `on_reset` — that is a
  backtest-between-runs concern and we don't backtest.

`start`/`stop` are `async` (real network I/O on the live path).

## Error model (crash-only, two classes)

- **Recoverable handler error** — a `Strategy`/`Feed` handler raises on a single event. Caught,
  logged with the correlation id, the component **continues**. One bad tick (or a third-party
  strategy bug) must not take down the engine.
- **Invariant violation** — an illegal saga transition, a failed checkpoint write, a broken
  guard contract. **Fail-fast → `FAULTED` → process exits → crash-only recovery** via
  snapshot-plus-reconcile (ADR-0009). This is the crash-only *"fail fast on invariant
  violations"* with graceful handling otherwise.

## Considered / deferred

- **`DEGRADED`** deferred: the exchange-reachable-but-erroring case is already modelled as
  reconciliation *behavior* (freeze the cycle, ADR-0011), so a `DEGRADED` state would be a
  second representation of the same fact. Add only when partial-health must be surfaced
  externally.
