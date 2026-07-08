# Observability is a first-class priority: structlog, ContextVar correlation, named events

Logging/observability is a **priority concern of this system**, not a cross-cutting afterthought.
A dedicated `observability/` module owns the correlation context and the named-event vocabulary,
and observability is part of the **behavior contract** — tests assert on named events, and
coverage of state-affecting paths is a requirement, not optional.

## Mechanics

- **structlog**, structured JSON output (human-formatted in dev, JSON for aggregation). Stdlib-
  grade performance is fine — latency is an explicit non-goal, so a Rust/MPSC
  dedicated-thread logger is out of scope.
- **`contextvars.ContextVar` correlation, auto-injected into every log line.** Two scopes: a
  **run id** (per process start) and a **per-operation correlation id** bound to the unit of
  work — the **`cloid`** while an order saga is in flight, the **`signal_id`** while a signal is
  processed, a **reconcile-cycle id** during reconciliation. Every line is traceable to the exact
  order/signal/cycle with no manual plumbing. This is the Python-native, finer-grained
  counterpart to coarse instance-id-in-filename correlation.
- **Named lifecycle events are first-class telemetry** — stable names emitted as structured
  records with an `event` field (not a separate pipeline). A documented catalog, e.g.:
  `signal.emitted`, `order.placed`, `order.submitted`, `order.live`, `order.partially_filled`,
  `order.filled`, `order.denied`, `order.rejected`, `order.failed`, `order.cancelled`,
  `saga.checkpoint`, `reconcile.started`, `reconcile.completed`, `reconcile.frozen` (connectivity
  guard), `reconcile.recency_skipped` (recent-order protection window), `inflight.reconciled`,
  `ghost.reconciled`, `fill.healed`, `feed.connected`, `feed.disconnected`,
  `guard.denied`, `strategy.snapshot`, `engine.faulted`.

  That list is the roadmap; the **shipped** catalog is the closed `observability.NamedEvent`
  enum, which grows one slice at a time (a name lands only with its emitting path and a
  catalog-walk test). Notable realized choices: the guard's pre-trade refusal ships as
  `order.denied` — one name for the `DENIED` terminal, shared with the other saga terminals —
  so the guard family currently ships only `guard.kill_switch_tripped`/`guard.kill_switch_reset`;
  `strategy.snapshot_incompatible` names the incompatible-restore path; the runner's
  startup-order proof ships as `engine.barrier_cleared`/`engine.feed_started` (the ADR-0024
  ordering, test-assertable) alongside the roadmap's `engine.faulted`.

## Coverage requirement

Every saga transition, every reconcile decision (including a `None`-read freeze), every guard
denial, every fill (venue or healed), and every component state change emits a named event with
the correlation id bound. The named-event catalog is documented and is a test-assertable
contract — a state-affecting path with no named event is a defect.
