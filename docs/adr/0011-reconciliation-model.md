# Reconciliation: two-phase, two-cadence, connectivity-guarded healing against the venue

Reconciliation compares local order state against the venue's truth and heals the difference.
It is the safety net that makes at-least-once delivery and crash recovery correct (ADR-0002,
ADR-0009).

## Structure

- **Two phases.** *Startup* = mass rebuild: load non-terminal orders from the durable store,
  query the venue by cloid (open orders **and fill history**), align state. *Continuous* =
  periodic loops thereafter.
- **Two cadences.** A **fast in-flight check** (default ~5s, bounded retries) resolves
  `SUBMITTED` orders that haven't acked — our riskiest "did it land?" path. A **slower
  open-order / ghost reconcile** (default ~30s) handles resting orders. Both configurable.

## Invariants (load-bearing — do not erode)

1. **Connectivity guard.** A failed venue read returns **`None`, never `[]`**. On `None` the
   cycle **freezes** — no order is ghosted or removed. An outage must never read as "all
   orders vanished."
2. **Cross-check before ghosting.** Before any terminal "gone" resolution, issue a targeted
   single-order/cloid query **and** consult fill history — a vanished order may have filled.
3. **Grace window.** An order must be **continuously absent across the grace window** (default
   ~90s ≈ 3 missed slow cycles) before it is ghost-resolved. Plus a **recent-order protection
   window**: skip orders whose last event is too recent to avoid racing the venue.
4. **Fill history is mandatory.** Venue open-orders endpoints exclude closed orders, so
   open-orders alone cannot distinguish "missing" from "recently closed." Always consult fill
   history (Hyperliquid `userFills`/`userFillsByTime`).
5. **Startup must succeed before trading.** If the venue is unreachable at startup, the engine
   does **not** begin placing orders (freeze, don't guess).
6. **Synthetic events are first-class.** Events the reconciler generates (a healed fill, a
   ghost rejection) carry a **deterministic id** and a **`reconciliation` flag**, so they are
   idempotent on replay and auditable as reconciler-sourced vs venue-pushed.
7. **Timing invariant.** The in-flight / fill-persist retry budget is capped **below** the
   ghost grace window, so an order still being retried can never be ghosted as missing.

## Resolutions

- In-flight `SUBMITTED` whose cloid the venue **positively has no record of** after exhausting
  queries → **`FAILED`** (proven non-landing; safe to recreate). We deviate from
  the common choice (resolving never-acked submits to `REJECTED`) because our ADR-0010
  taxonomy has a dedicated `FAILED` terminal that expresses this honestly; `REJECTED` is
  reserved for an actual venue refusal.
- An order ghosted after the grace window + cross-check → **`REJECTED`** (ghost-reconciled) if
  truly gone, or **`FILLED`/`PARTIALLY_FILLED`** if the cross-check found fills.

## External orders (boundary rule)

The engine manages **only orders it placed**, recognized by its own cloid. An order found at
the venue with an unrecognized cloid (manual UI order, leftover from another run on the same
wallet) is **logged and flagged external — never acted on**: never cancelled, never fed to a
strategy. The cloid is the ownership boundary. A claimable-external-order
mechanism is deferred — meaningless without a multi-strategy ownership model (out of v1 scope).
This case cannot arise on the paper exchange; it is a live-path-only concern.

This reflects established live-reconciliation practice (startup mass-status + continuous in-flight
monitoring, query-failure-vs-empty distinction, single-order re-query before terminal
resolution, recent-order protection) and the author's prior ghost-reconciler + fill-history
backstop — reimplemented from first principles, no code carried.
