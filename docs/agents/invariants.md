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
3. **Reconciliation freezes on connectivity failure.** A failed venue read returns a
   `VenueReadFailure`, never a view and never `[]`; nothing heals on one — no order is ghosted
   or removed. An outage must never read as "all orders vanished." *At the account grain the same guard is
   `fetch_account_state() -> VenueAccountState | None`, and `None` means no venue truth to
   compare against, never a flat book: nothing heals. A response the adapter cannot **parse**
   is a failed read too, not an empty account — including one that parses *cleanly* into a
   figure the engine cannot stand behind. `Decimal("nan")`/`Decimal("Infinity")` are valid
   constructions, and a figure re-typed as a JSON number lost digits and scale to `float` in
   `json.loads` before any parse of ours saw it, so neither announces
   itself; every boundary that reads a reported figure passes it through one guard and turns
   the refusal into its own layer's failed read (a dropped frame on a feed, a named
   `VenueReadFailure` on a venue read). `domain.exact_figure` holds the universal half — a
   figure must be a number — and each venue owns what its own figures may be *encoded* as,
   Hyperliquid's being `venues/hyperliquid/reading.py` (`figure`, plus the `UNREADABLE`
   vocabulary every grain of that venue catches). Paper's `None` is its permanent answer,
   because it holds no account state at all, so a cadence mistakenly pointed at it freezes
   rather than healing a restored ledger to flat. At the **startup barrier** the same `None`
   costs more than a frozen cycle: the live-only account materialisation retries inside the one
   startup budget and then **faults** the process, because clearing the barrier with no account
   row is the state that step exists to prevent — freeze-don't-guess applied to the cash line,
   and what keeps ADR-0041 §6's "`cash` is never `None`" true rather than merely intended. Both
   freezes at this grain are **recorded**, under one name with a `step` field
   (`account.reconcile_frozen`, `barrier` | `cadence`): a step that touches nothing and says
   nothing reads exactly like a cadence that stopped running, and at the barrier left an operator
   with `engine.faulted` and no record naming the read that caused it.*
   **The boundary of this invariant is permanence.** A failed read and a retry is the answer
   for a read that *may* succeed later — a dead transport, an unreadable body — with the
   per-cloid span below as how that retrying finds out whether waiting helps. A venue fact this
   engine cannot represent (a fill fee settled in a token other than USDC) is already stored at
   the venue and reads back identically forever, so it is durable at the *first* read and the
   span has nothing to discover: answering it that way spends the whole span re-reading it and
   then faults naming only the cloid, where the refusal itself can name the offending fact.
   Those refuse as `VenueFactUnsupported`, deliberately outside the
   `UNREADABLE` vocabulary every transient guard catches, and **fault** the engine. One venue
   read, three outcomes, mapped once per venue (`venues/hyperliquid/reading.py`). What a read
   is covered by is **whether something above it retries** — a barrier step freezes because the
   barrier re-drives it, while a read with no retry above it (`universe.py`, the ADR-0046 mode
   gate) owns its own refusal and raises.
   **How much one failed read costs is the second half**, and why the failure is a two-member
   type: a failed **send** stops the pass (no body arrived, so the venue may be unreachable and
   every order behind it would pay a 30s request timeout to learn the same), while an unreadable
   **body** — from a venue that is up and answering — skips only its own order and the pass
   reconciles the rest. `_drive` may never again let one order's failure stop the orders behind
   it on a venue that answered. The durable case is caught by a per-cloid **span of continuous
   unreadability** (`unreadable_grace_seconds`), escalating to `VenueReadUnresolvable`, which
   **faults** rather than inventing a terminal state for an order whose body was never read; it
   restarts on any readable read, so blips can never accumulate into a fault. The span is
   wall-clock and **never a read count**: its three drivers poll 5s, 30s and the startup
   barrier's backoff apart, so a count buys a different amount of waiting under each — and at
   boot would silently overrule `startup_reconciliation_timeout_seconds`. The pass verdict stays
   `False` on either failure, so a startup pass that could not prove an order never clears the
   barrier (inv 5). (ADR-0011, ADR-0034, ADR-0043 §6, ADR-0048, ADR-0049)
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
   *Enforced from the ledger's own recovery step — the `Store` binds its ledger to the account's
   whole opening declaration (`account_id`, plus the genesis collateral a paper ledger was opened
   at) and fail-fasts when the adapter or config reports another, one error naming every field that
   disagrees; the same error also refuses a paper store holding order history but no ledger, which
   cannot be backfilled. The check runs before any other recovery work. Concurrent ownership is
   undetectable in-process either way and stays a deployment rule.*
   (ADR-0038, ADR-0042, ADR-0043, ADR-0031, ADR-0034)
8. **The live account's abstraction mode is Manual/Standard.** Every account-grain number this
   engine reconciles against assumes the venue's perps account snapshot *is* the account; under a
   pooled mode it is only the collateral posted into perps, so equity and free margin read an order
   of magnitude low with no field indicating it. Verified at **boot** (an allowlist of `default` /
   `disabled`; anything else, or an unreadable mode, refuses to start) and re-verified **before any
   Tier-1 account-cash heal** — where a mode that is changed **or unverifiable** (a failed read, an
   unrecognised literal) refuses the heal and freezes the account-grain reconcile, because healing
   would write a sub-ledger's value into the durable cash line. *The guard fails closed at both
   points: an unverified mode is never read as an unchanged one. Live-only; paper has no venue and
   no mode.*
   (ADR-0046, ADR-0034, ADR-0040, ADR-0042, ADR-0043)
