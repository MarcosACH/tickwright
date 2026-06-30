# Order terminal taxonomy: DENIED vs REJECTED vs FAILED (refines ADR-0007)

The order saga has three *negative* terminals, split on the axis recovery and telemetry
actually care about — **was the order sent, and who decided?** — rather than one reason-coded
`REJECTED`:

- **`DENIED`** — refused by our **pre-trade guard** (min-notional/qty, kill-switch); **never
  sent** to the venue. Safe to recreate as a fresh intent immediately.
- **`REJECTED`** — **sent**, and the **venue** adjudicated and refused it (includes the
  ghost-reconciled case: an order that was live at the venue then vanished). Venue-final.
- **`FAILED`** — **sent (or attempted)** and we have positive proof it **never landed**
  (hard transport/local error). May warrant a retry/replace decision.

Plus the positive terminals `FILLED` and `CANCELLED`. Total: 9 states.

Updated negative transitions:

```
PENDING ─guard deny──▶ DENIED                    (pre-trade, never sent)
SUBMITTED ─venue no──▶ REJECTED                  (sent, venue refused)
SUBMITTED ─hard fail─▶ FAILED                    (sent, proven non-landing — NOT a timeout)
LIVE | PARTIALLY_FILLED ─ghost reconcile─▶ REJECTED
```

This adopts the established `DENIED` (pre-trade) vs `REJECTED` (venue) distinction —
*"each order event corresponds to a state transition in the order state machine"* — which we
had collapsed in ADR-0007. For a reference implementation people read to learn the canonical
model, vocabulary parity with the source of truth is worth the extra terminal. The "was it
sent?" axis is also the precise question reconciliation asks on restart.

Supersedes the terminal-set and the `PENDING → REJECTED(guard)` transition of ADR-0007; all
other ADR-0007 content (the non-terminal states, the two load-bearing invariants) stands.
