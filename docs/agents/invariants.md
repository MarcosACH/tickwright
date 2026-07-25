# Tickwright invariants (review-blocking)

The cross-cutting behavioral invariants that reviews and refactors must not regress. Each is
decided in an ADR — **the ADR is the source of truth**; this file is the checklist-shaped
index. If an entry here disagrees with its ADR, the ADR wins: fix this file.

Consumed by `/code-review` (any regression is BLOCKING) and `/python-codebase-mastering`
(behavior lock during refactors). Do not copy this list into skills or issues — link it.

1. **Saga idempotency.** Delivery is at-least-once; replaying a delivered event must not
   double-place or double-cancel an order. Consumers are idempotent by construction.
   (ADR-0002, ADR-0025)
2. **Crash-safe recovery.** The `PENDING` intent is written ahead of the send; recovery is
   snapshot-plus-reconcile and a saga checkpoint replayed after restart converges to the same
   terminal state. (ADR-0008, ADR-0009)
3. **Reconciliation freezes on connectivity failure.** A failed venue read returns `None`,
   never `[]`; on `None` the cycle freezes — no order is ghosted or removed. An outage must
   never read as "all orders vanished." (ADR-0011)
4. **Rejections are explicit events.** A placed-but-rejected order surfaces as its taxonomy
   terminal (`DENIED` / `REJECTED` / `FAILED`), propagated as an event — never a silent
   `return None`. (ADR-0010)
5. **Per-symbol ordering end-to-end.** Events are keyed by symbol; one symbol's causal chain
   stays on a single ordered timeline, and a mismatched routing key must not silently drop or
   misdirect events. Cross-symbol ordering is never relied upon. (ADR-0003, ADR-0023)
6. **Deterministic paper exchange.** Fills and rejections are reproducible from the same input
   sequence and clock; all time flows through the injected `Clock`. (ADR-0012, ADR-0005)
7. **Account exclusivity.** One process trades exactly one account, and an account is owned by
   exactly one process — the cloid ownership boundary protects the order saga but has no analogue
   on a position, so two engines on one account each heal their ledger toward the other's flow.
   *Enforcement lands with the accounting surface — the `Store` binds its ledger to the account's
   whole opening declaration (`account_id`, plus the genesis collateral a paper ledger was opened
   at) and fail-fasts when the adapter or config reports another, one error naming every field that
   disagrees; concurrent ownership is undetectable in-process either way and stays a deployment
   rule.*
   (ADR-0038, ADR-0042, ADR-0031, ADR-0034)
