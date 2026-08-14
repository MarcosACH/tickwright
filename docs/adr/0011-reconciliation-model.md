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

1. **Connectivity guard.** A failed venue read returns a **`VenueReadFailure`, never a view and
   never `[]`**. Nothing heals on one — no order is ghosted, removed or counted. An outage must
   never read as "all orders vanished." *A dead transport and a body the adapter cannot parse
   are both failed reads; what is **not** covered is a venue fact the engine cannot represent,
   which a retry re-reads identically forever and which therefore faults rather than freezing —
   [ADR-0048](./0048-venue-read-outcomes.md) fixes the three outcomes and where they are mapped.
   How much of a pass one failed read costs turns on **which** of the two it is, which is why
   the failure is a two-member type rather than one sentinel: a failed send stops the pass (the
   venue may be unreachable, and every order behind it would pay a request timeout to learn the
   same), while an unreadable body — from a venue that is up and answering — skips only its own
   order, against a per-cloid **span of continuous unreadability** that faults the engine once
   the condition is proven durable. The span is wall-clock and not a read count because its
   three drivers poll as far apart as 5s, 30s and the startup barrier's backoff, so a count
   would mean a different amount of waiting under each ([ADR-0049](./0049-failed-read-blast-radius.md)).*
2. **Cross-check before ghosting.** Before any terminal "gone" resolution, issue a targeted
   single-order/cloid query **and** consult fill history — a vanished order may have filled.
3. **Grace window.** An order must be **continuously absent across the grace window** (default
   ~90s ≈ 3 missed slow cycles) before it is ghost-resolved. Plus a **recent-order protection
   window** (default ~30s ≈ one slow cycle): skip ghost evaluation for orders whose last saga
   event is too recent — the grace clock never arms — to avoid racing the venue's not-yet-
   propagated open-orders snapshot. The fill-history cross-check still runs inside the window.
4. **Fill history is mandatory.** Venue open-orders endpoints exclude closed orders, so
   open-orders alone cannot distinguish "missing" from "recently closed." Always consult fill
   history (Hyperliquid `userFills`/`userFillsByTime`).
5. **Startup must succeed before trading.** If the venue is unreachable at startup, the engine
   does **not** begin placing orders (freeze, don't guess).
6. **Synthetic events are first-class.** Events the reconciler generates (a healed fill, a
   ghost rejection) carry a **deterministic id** and a **`reconciliation` flag**, so they are
   idempotent on replay and auditable as reconciler-sourced vs venue-pushed.
7. **Timing invariant.** The in-flight / fill-persist retry budget **and** the recent-order
   protection window are each capped **below** the ghost grace window, so an order still being
   retried can never be ghosted as missing, and the protection pre-filter can never outlast the
   grace measurement it precedes. Both bounds are enforced at `ReconcileConfig` construction.

## Resolutions

- In-flight `SUBMITTED` whose cloid the venue **positively has no record of** after exhausting
  queries → **`FAILED`** (proven non-landing; safe to recreate). We deviate from
  the common choice (resolving never-acked submits to `REJECTED`) because our ADR-0010
  taxonomy has a dedicated `FAILED` terminal that expresses this honestly; `REJECTED` is
  reserved for an actual venue refusal.
- An order ghosted after the grace window + cross-check → **`FILLED`/`PARTIALLY_FILLED`** if the
  cross-check found fills; if truly gone, **`REJECTED`** from `LIVE` and **`CANCELLED`** (fills
  preserved) from `PARTIALLY_FILLED` (ADR-0010).

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
