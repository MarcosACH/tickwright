# Saga checkpointing: write-ahead before send, reconcile-by-cloid before resend

Every order saga transition checkpoints durably (throughput is not a goal; readable recovery
is). Two hard ordering rules make it crash-safe:

1. **Write-ahead the intent.** The `PENDING` record (cloid + full order params) is persisted
   **before** the network send — never after. A crash anywhere after this leaves a durable
   record that a placement with *this cloid* was intended, so recovery can reconcile by cloid
   instead of being blind to an order it may have placed.
2. **Reconcile-by-cloid before resend.** Because Hyperliquid does not guarantee cloid dedup,
   recovery of any non-terminal order queries the venue by cloid (open orders + fill history)
   and **resends only if the venue has no record of that cloid**. This is what stops the
   write-ahead log from causing double-places.

Checkpoint points: create `PENDING` (before send); `→ SUBMITTED` (state + send timestamp,
which arms the in-flight grace clock); `→ LIVE`; each `→ PARTIALLY_FILLED` (state + cumulative
filled qty); each terminal transition (with reason). The separate `SUBMITTED` checkpoint is
kept deliberately — its send timestamp is what bounds the in-flight grace window; losing it
widens the double-send race.

## Accepted residual risk

One window is irreducible: a send in-flight on the wire when the process crashes, which the
venue may or may not have received. Reconcile-before-resend plus the reconciliation grace
window (sized in the reconciliation ADR) **mitigate** this; they do not eliminate it. Named
here so it is a known, bounded risk rather than a hidden one.
