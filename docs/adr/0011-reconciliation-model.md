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
   **(Amended by #242 — the cross-check is keyed by the ack's oid and bounded by the ack time:**
   Hyperliquid drops the `orderStatus` record after about the account's last 2000 orders, by
   count. Fills stay for years. So an active account gets `unknownOid` for an order whose fill
   is still on the books, and lookup by cloid fails at the same moment lookup by oid does
   (`docs/research/hyperliquid-order-status-retention.md`). The order record is
   therefore not where the cross-check can come from once it is gone. The saga keeps the oid
   and the time from the first `LIVE` ack (`Order.venue_oid`, `Order.acked_ts_ns`), and the
   read carries both to the venue in an `OrderRef`. On `unknownOid` the adapter reads
   `userFillsByTime` from the ack time less a 60 s skew allowance, keyed by that oid, and
   answers a view with no status and those fills. A database written before the time was kept
   gets the column on open, backfilled from each row's `LIVE` checkpoint time. So a saga with
   an oid but no ack time is one that never checkpointed as `LIVE`. That case has no bound to
   read from, and it falls back to `userFills`, the venue's last 2000 fills.
   A failed fills read is the failure, never an empty view (inv 1). [#242]**)**
   **(Amended by #328 — the ack is whichever placement answer carries an oid:**
   Hyperliquid answers a placement with `resting` or `filled`, and both carry the venue's oid.
   Only `resting` used to publish the `LIVE` ack. A `filled` answer went straight to the fills
   read, so the saga never kept the oid. If that read failed, the saga sat `SUBMITTED` with no
   oid. Once the venue dropped the record, the cross-check above had no key, and the inflight
   budget resolved a filled order `FAILED`. Now a `filled` answer publishes the same `LIVE` ack
   first, then its fills. `LIVE` means the venue acked the order with its oid, not that it
   rested. A failed fills read leaves the saga `LIVE`, where the open-order cadence heals it by
   the oid within one slow cycle. The adapter does not retry the read. Reconcile owns retries
   (ADR-0008). The paper venue has no oid and keeps its direct `SUBMITTED → FILLED` path. [#328]**)**
   **(Amended by #354 — a cloid can name more than one venue order, so every read and cancel
   goes by the oid once there is one:**
   The cloid is derived from the signal id (ADR-0006), and the signal seq restarts at 1 in a
   fresh store. The venue remembers every cloid ever placed on the account. So a second life of
   the account places the same cloid again, and `orderStatus` by cloid may answer with either
   order. Which one it answers while the new one is still landing is not documented. The
   engine adopted an earlier life's record and its fills as this saga's, then faulted when the
   real fill arrived. Three rules close that. The saga stamps its own creation time
   (`Order.created_ts_ns`, backfilled from the first checkpoint on an older database). Once the
   saga holds an oid, the record read and the cancel go by that oid, which names exactly one
   order. Before the ack there is no oid, so the read goes by cloid, and a record the venue
   placed before the saga's creation time, less the same 60 s skew allowance, reads as no
   record. Its fills are never read. To the in-flight budget that is one miss, not a landing.
   A cancel before the ack still goes by cloid. Which order the venue cancels then is its
   choice, and reconciliation is the backstop. [#354]**)**
3. **Grace window.** An order must be **continuously absent across the grace window** (default
   ~90s ≈ 3 missed slow cycles) before it is ghost-resolved. Plus a **recent-order protection
   window** (default ~30s ≈ one slow cycle): skip ghost evaluation for orders whose last saga
   event is too recent — the grace clock never arms — to avoid racing the venue's not-yet-
   propagated open-orders snapshot. The fill-history cross-check still runs inside the window.
   **(Amended by #243 — this invariant governs the startup pass too:**
   it was silently read as exempting it.
   The mass rebuild used to ghost a `LIVE` saga on the *first* absent read and report
   the pass successful — terminal, since the `StartupBarrier` only re-drives a step that returns
   `False`, so there was no second look. Boot may not answer "is this order gone?" more
   aggressively than the running engine does, and boot is where it has the least standing to:
   a restart moments after a placement ack reads a venue whose record has not propagated to the
   read node, which is the exact race the second clause exists to prevent. The counter-argument
   in the code — the venue once ACKed it, so an empty read means gone — is true and applies
   verbatim at runtime, where the engine nonetheless waits the window out. Startup now puts an
   absent resting order to the same `GhostGate`: it **arms** the grace clock rather than
   concluding on it, and the pass still returns `True`, because the read succeeded and only the
   verdict is deferred. Freezing instead would spend the whole window and then fault startup on
   one absent order, which is a worse trade than a slower first ghost. The window is therefore
   measured from the boot instant, so a boot that re-drives across it ghosts on the startup pass
   itself; a record that returns first resets it, like any other presence. What boot cannot use
   is the **protection** clause: `Cache.rebuild()` deliberately clears `_last_event_ns`, so every
   recovered saga reads `None` recency and the pre-filter has nothing to protect with — grace is
   boot's only guard, and that is now the stated reason rather than an accident of the rebuild.
   Unchanged: a `PENDING`/`SUBMITTED` saga the venue has no record of still resolves `FAILED` on
   the startup pass, a different resolution under *Resolutions* below. [#243]**)**
4. **Fill history is mandatory.** Venue open-orders endpoints exclude closed orders, so
   open-orders alone cannot distinguish "missing" from "recently closed." Always consult fill
   history (Hyperliquid `userFills`/`userFillsByTime`).
   **(Amended by #242 — a lost ack has no fill history to consult:**
   fills are keyed by the venue's oid, and the saga only learns the oid from the ack. A
   `SUBMITTED` saga whose ack never arrived has no oid, so on `unknownOid` the adapter answers
   an empty view and sends no fills request. The inflight budget resolves it `FAILED`, as
   before. Recent fill rows carry an undocumented `cloid` field. It is absent on older fills and
   on some accounts, so it is not a key the read may rely on. [#242]**)**
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
