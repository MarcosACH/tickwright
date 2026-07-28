# Component lifecycle + error model: 4-state, crash-only, two error classes

Every long-lived component (`MarketFeed`, `Strategy`, `Exchange`, the `Engine` host) shares one
lifecycle, a readable component-state FSM:

- Methods: `async start()`, `async stop()`, a read-only `state` property, `health()`.
- States: **`READY → RUNNING → STOPPED`**, plus **`FAULTED`** (unrecoverable). `DEGRADED`,
  `DISPOSED`, and the transitional states are **deferred**.
- Override hooks: `on_start()` / `on_stop()` (subscribe / clean up). No `on_reset` — that is a
  backtest-between-runs concern and we don't backtest.

`start`/`stop` are `async` (real network I/O on the live path).

**(Noted by ADR-0044 §7:** the `Exchange` Protocol declared none of this until ADR-0044, which adds
**`start()` only** — the venue-alignment step ADR-0024's step 4 always named. `stop()`, `state` and
`health()` stay undeclared on that Protocol until there is teardown to do: the live adapter holds no
persistent connection of its own, posting per request. The contract above is unchanged; only the
`Exchange` Protocol's coverage of it has moved.**)**

**(Amended by [#186](https://github.com/MarcosACH/tickwright/issues/186):** `stop()` is now declared
too, and ahead of the teardown that motivates it. ADR-0044 §7's condition is met by ADR-0037's paper
funding generator — a `Clock`-driven task inside `PaperExchange`, which is teardown to do — but the
pair lands *as a pair*, in one prefactor: four later slices hang a boot guard or a background loop
off these two verbs, and the runner ordering is precisely the thing none of them should re-litigate
separately. Both adapters implement `stop()` as a no-op until that generator exists. `state` and
`health()` stay undeclared, on the original condition.**)**

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
