# A stray order is cancelled, never adopted

A client timeout does not prove the venue never got a place request. So the venue can accept an
order after the engine resolved its saga `FAILED`. A send from a previous life can do the same
after boot. Before this ADR nothing looked at that order again. The open-order pass read only
non-terminal sagas, and the account reconcile did not compare the venue's open orders with the
books. If the order filled, the engine held a position it did not plan. Decided in a
`/grill-with-docs` session on 2026-11-12 for
[#642](https://github.com/MarcosACH/tickwright/issues/642).

A **stray order** is a venue order that rests under a cloid whose saga is terminal. An **external
order** is a venue order the engine did not place. Both terms are in `CONTEXT.md`.

## Decision

- **Cancel, never adopt.** The saga stays terminal. Reopening it would break "terminal is
  terminal" (ADR-0010, ADR-0026). The strategy already got its terminal and may have replaced the
  order, so adopting it could double the exposure.
- **Every terminal state counts except `DENIED`.** A `DENIED` saga never sent its place. A venue
  order under its cloid did not come from us, so it is external.
- **An unknown cloid is an external order.** So is an order with no cloid. The engine logs it once
  per oid per process life and never acts on it. This keeps the ADR-0011 boundary rule. Only an
  operator `cancel-all` or `flatten` may cancel it (ADR-0052).
- **The list is not proof, so each candidate is read again.** The pass reads `fetch_order` by the
  saga's own `OrderRef`.
  - Still resting: cancel it.
  - No record, because the adapter refused a record older than the saga (invariant 12): it belongs
    to an earlier life. Log it as external.
  - No longer resting: do nothing.
  - Read failed: as in the per-order passes (ADR-0049). A failed send freezes the cycle. A rate
    limit freezes it and returns `RATE_LIMITED`. An unreadable body skips that order and charges
    its own unreadable span.
- **The Reconciler cancels directly.** It sends the cancel on its `OrderAnchor`, by the oid from
  the confirm read. No saga is written. The pass keeps no memory, so a lost cancel is sent again
  on the next pass.
- **Fills on a stray are logged, not booked.** Booking one would move a terminal saga, an illegal
  transition that faults the engine. Ledger reconciliation books the position as unattributed.
  The cancel line names the fills behind it.
- **It runs on the open-order cadence only.** One `fetch_open_orders` per pass, after the
  per-order reads, plus one confirm read per candidate. The `Reconciler` takes the account anchor
  for the list read. The startup barrier does not change.
- **A failed list read follows ADR-0049.** A failed send freezes the cycle. A rate limit freezes it
  and returns `RATE_LIMITED`. An unreadable list freezes the cycle and is charged to the
  `unreadable_grace_seconds` span, keyed on the account. Past the span the pass raises
  `VenueReadUnresolvable`. The pass is `PROVED` only if the list read succeeded too.
- **Named events.**
  - `reconcile.stray_cancelled`: every cancel sent. It carries the saga state, the oid, and the
    unbooked fills as trade ids, total quantity and total fee.
  - `reconcile.external_order`: once per oid per process life.
  - `order.place_answer_after_terminal`: a place answer reaches a terminal saga. The check lives
    in the `ExecutionManager`. A `LIVE` report outside reconciliation is only ever a place answer,
    so that is what it matches. The saga does not move.
- **Paper gets no new setting.** Paper cannot make a stray, since a saga stays open while its send
  is (#640). A paper test proves the pass runs clean there.

## Considered options

- **Adopt the order into its saga.** Rejected. It reopens a terminal saga, and the strategy may
  already have acted on that terminal.
- **Leave it and only log it.** Rejected. A resting order the engine does not track can fill at any
  time, into a position nobody planned.
- **Cancel straight from the list.** Rejected. The list is a snapshot and can be stale. It also
  cannot tell this saga's order from an earlier life's order under the same cloid.
- **Remember the cancels already sent.** Rejected. A pass that keeps no memory sends a lost cancel
  again, and a restart loses nothing.
- **Check on every cadence and at boot.** Rejected. The open-order cadence is the one that already
  reads resting orders. One more list read per pass is cheap.

## Consequences

- A stray rests for at most one open-order interval (30 s by default) after the venue lists it.
- A late order that fills in full never rests, so this pass never sees it. Only ledger
  reconciliation does, as an unattributed position.
- `order.place_answer_after_terminal` does not fire for a place request that timed out on our side.
  That answer never reaches the engine. In practice it fires when a fill arrives before the place
  answer. The stray pass is what catches the timeout case.
- New invariant 14 in `docs/agents/invariants.md`.
