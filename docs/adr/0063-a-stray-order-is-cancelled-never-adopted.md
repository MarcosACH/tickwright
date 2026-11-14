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
  - Another order listed under the same cloid, with an oid other than the one the read names:
    it belongs to an earlier life (#354). Log it as external.
  - No longer resting: do nothing.
  - Read failed: as in the per-order passes (ADR-0049). A failed send freezes the cycle. A rate
    limit freezes it and returns `RATE_LIMITED`. An unreadable body skips that order and charges
    its own unreadable span. That span is kept apart from the saga's working life, so a run left
    open there never counts. A list read that no longer names the order ends its run.

  **(Amended by #652 — a saga with no oid is read once per listed oid:**
  The venue lets a second order rest under a cloid while the first one still rests. Which one a
  read by cloid answers with is not documented (ADR-0011, #354). If it answered with an earlier
  life's order, the age check refused it. This life's stray was then logged as external and
  never cancelled. So a saga with no oid now reads each oid the list names under its cloid,
  through `OrderRef.listed_oid`. The saga does not hold that oid, so the age check still runs.
  Each read judges only its own listed order. Still resting: cancel it. Refused by the age
  check: external. No longer resting: do nothing. If two listed orders both pass the age check,
  both are cancelled. That happens when an earlier life placed the cloid within the 60 s skew
  allowance. It is the one case where the engine may cancel an earlier life's order. A cancel
  never adds exposure, and our own stray left resting could fill. An unreadable read is charged
  to a span keyed by its listed oid, so a good read of one order never restarts another's span.
  A saga with an oid still reads by it. A list entry with no oid, as on paper, keeps the one
  read by the saga's own ref. [#652](https://github.com/MarcosACH/tickwright/issues/652)**)**
- **The Reconciler cancels directly.** It sends the cancel on its `OrderAnchor`, by the oid from
  the confirm read. No saga is written. The pass keeps no memory, so a lost cancel is sent again
  on the next pass.
- **Fills on a stray are logged, not booked.** Booking one would move a terminal saga, an illegal
  transition that faults the engine. Ledger reconciliation books the position as unattributed.
  The cancel line names the fills behind it. A fill the saga booked before it went terminal is in
  the books already, so the line leaves it out.
- **It runs in the open-order pass only.** That pass runs on the open-order cadence, and on each
  pass a flatten run drives (ADR-0060). One `fetch_open_orders` per pass, after the per-order
  reads, and only when they all proved. Then one confirm read per terminal saga the list names.
  The `Reconciler` takes the account anchor for the list read. The startup barrier does not
  change.
- **A failed list read follows ADR-0049.** A failed send freezes the cycle. A rate limit freezes it
  and returns `RATE_LIMITED`. An unreadable list freezes the cycle and is charged to its own
  window of the `unreadable_grace_seconds` span. Past the span the pass raises
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

**(Amended by #652 — the listed oid stays a field on `OrderRef`:**
`OrderRef` names what the saga holds, and the saga does not hold a listed oid. So the review of
PR #653 tried a `fetch_order(ref, *, listed_oid=None)` argument instead. Rejected. mypy then
reported 264 errors in 36 files, and all but `app/build.py` were tests. The field's risk is small.
Only `fetch_order` reads it. A ref that carried it into `cancel` would cancel by cloid. That is how
an unacked saga's cancel already works. No caller builds such a ref, since the stray cancel builds
a fresh ref from the saga. The `OrderRef` docstring states this. Revisit this if a method other than
`fetch_order` ever needs the field.
[#653](https://github.com/MarcosACH/tickwright/pull/653)**)**

## Consequences

- A stray rests for at most one open-order interval (30 s by default) after the venue lists it,
  while the pass's reads succeed. A failed read holds the stray check back until a pass proves.
- A late order that fills in full never rests, so this pass never sees it. Only ledger
  reconciliation does, as an unattributed position.
- `order.place_answer_after_terminal` does not fire for a place request that timed out on our side.
  That answer never reaches the engine. In practice it fires when a fill arrives before the place
  answer. The stray pass is what catches the timeout case.
- New invariant 14 in `docs/agents/invariants.md`.
