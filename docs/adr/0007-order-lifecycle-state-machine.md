# Order-lifecycle saga: a readable order-state FSM

> **Note:** the terminal set below was refined by **ADR-0010** — the single `REJECTED`
> (guard + venue) is split into `DENIED` (pre-trade, never sent) / `REJECTED` (venue) /
> `FAILED`. The non-terminal states and the two load-bearing invariants here still stand.

The order saga is an explicit finite state machine. States: `PENDING` (intent recorded, cloid
assigned, not yet sent), `SUBMITTED` (sent, awaiting ack — the in-flight state), `LIVE`
(accepted/working), `PARTIALLY_FILLED` (some qty filled, remainder working), and the terminal
`FILLED`, `CANCELLED`, `REJECTED`, `FAILED`.

Legal transitions:

```
PENDING ─submit──────▶ SUBMITTED
PENDING ─guard reject▶ REJECTED                  (pre-trade min-notional/qty guard, kill-switch)
SUBMITTED ─ack rest──▶ LIVE
SUBMITTED ─immediate─▶ FILLED | PARTIALLY_FILLED (paper immediate / marketable)
SUBMITTED ─venue no──▶ REJECTED
SUBMITTED ─hard fail─▶ FAILED                    (proven non-landing, NOT a timeout)
LIVE ────────────────▶ PARTIALLY_FILLED | FILLED | CANCELLED | REJECTED(ghost)
PARTIALLY_FILLED ────▶ FILLED | CANCELLED | REJECTED(ghost)
```

This is the smallest subset of a full order FSM where every state still maps to a
*distinct recovery situation*; we drop their richer surface (`EMULATED`, `PENDING_UPDATE`,
`EXPIRED`, a separate `PENDING_CANCEL`, …) as out of scope. Cancel is modelled `LIVE →
CANCELLED` with any in-flight cancel resolved by reconciliation, not a `CANCELLING` state.

## Load-bearing invariants

1. **A timeout is never a direct transition.** A crash/timeout in `SUBMITTED` leaves the order
   in `SUBMITTED`; only reconciliation against the `cloid` may move it. `FAILED` requires
   positive proof the order never landed. This is the order-level connectivity-failure guard
   (the prior system's "`None`, never `[]`" rule).
2. **`REJECTED` ≠ `FAILED`.** `REJECTED` = a request that was actually adjudicated and refused
   (guard or venue). `FAILED` = our side proved the order never landed. The recovery semantics
   differ, so they stay distinct rather than collapsing into one reason-coded terminal.

`PARTIALLY_FILLED` is **retained** (confirmed by ADR-0012): the `StochasticFillModel` exercises
it; the default `ImmediateFillModel` always fills fully.
