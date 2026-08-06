# The blast radius of a failed read: how much of a pass one order may cost

_Recorded while delivering [#236](https://github.com/MarcosACH/tickwright/issues/236), filed after the [ADR-0048](./0048-venue-read-outcomes.md) work rescued the foreign-token fee from a permanent freeze and left the rest of the `UNREADABLE` tuple standing in it. **Extends ADR-0011 inv 1** — the `None` contract becomes a two-member failure type, and the freeze it triggers gains a grain. **Completes ADR-0048 §2**: the three facts that made the fee a poison pill are the same three facts here, and this is where facts 2 and 3 are actually removed rather than routed around. Supports [ADR-0014](./0014-component-lifecycle-and-error-model.md) (what a violated invariant does) and [ADR-0024](./0024-engine-runner-lifecycle-and-supervision.md) (where the fault lands)._

A failed read stopped a whole pass because the reconciler could not tell which failure it had.

## 1. Decision

**A failed venue read costs the rest of the pass only when the venue did not answer, and one order may be unreadable for only so long.**

Two changes, one to each half of the problem:

| | Failed **send** (`SEND_FAILED`) | Unreadable **body** (`UNREADABLE_BODY`) |
| --- | --- | --- |
| What is known | No body arrived; the venue may be unreachable | The venue is up and answered promptly, unreadably |
| Cost to the pass | The pass stops — nothing behind it is read | That order is skipped; every order behind it reconciles |
| Repeated on one cloid | Nothing accumulates | A per-cloid budget, then `VenueReadUnresolvable` faults the engine |

`Exchange.fetch_order` returns `VenueOrderView | VenueReadFailure` and `venues/hyperliquid/reading.py`'s `read` carries the distinction out of the one place that already had it. The pass verdict is `False` on either failure, so ADR-0011 inv 5 is untouched.

## 2. What was wrong: a fix that only moved the poison

ADR-0048 §2 lists three facts that must line up before a transient verdict on a permanent condition becomes a silent, permanent stall:

1. the venue's stored value is immutable, so the read fails identically on every pass;
2. `_drive` returns its freeze *before* `handle` runs, so `_resolve_inflight`'s `ConsecutiveMisses` budget never advances;
3. `_drive` returns on the first frozen read, so the affected order freezes **every order behind it** in the iteration too.

ADR-0048 removed fact 1 for exactly one condition — a fee settled in a token the ledger cannot hold — by making it a `VenueFactUnsupported` that leaves the venue seam. **Facts 2 and 3 were untouched**, and fact 1 holds for the rest of the `UNREADABLE` tuple just as well: a venue status string outside our taxonomy is stored at the venue and read back identically forever, a renamed response field the same. Issue #236 drove the real `Reconciler` against the real `HyperliquidExchange` with a faked POST boundary and got five cycles, five `RECONCILE_FROZEN`, and zero reads of the healthy order behind it — and reproduced identically on **both** continuous cadences, because both are `_drive`.

So the taxonomy was right and incomplete. It fixed the one condition it could name and left the shape of the failure — one order stopping every other, forever — in place for everything it could not.

## 3. Why the split is the fix and not "continue the loop"

"Continue instead of returning" was the obvious remedy and is wrong on its own. `transport.py` bounds each request at 30s total, so against a genuinely dead venue a continuing loop pays N × 30s per cycle to learn N times what the first read already showed. The early return is protecting something real.

What makes the cheap discrimination available is that **the information already existed and was being discarded one line later**. `read` catches the two causes in two separate `try` blocks, names them differently, and quotes the body for one of them — and then returns `None` for both. Recovering it costs nothing:

- **no body arrived** ⇒ the venue's reachability is in doubt, every other order in the worklist is about to pay a full request timeout to re-test the same connection, and stopping is the honest economy. Unchanged behaviour.
- **a body arrived and could not be read** ⇒ the venue is reachable, fast, and answering. Its answer for *this* cloid says nothing whatsoever about the next cloid's. Skipping is one ordinary round-trip; stopping costs every other order, on every cycle, for as long as the venue keeps answering the same way.

This is fact 3 removed at the root, for the whole `UNREADABLE` tuple rather than one member of it.

### 3.1 Why the return type widens, given inv 1 narrowed it

ADR-0011 inv 1 says a failed read returns `None`, **never `[]`** — the guarantee is that a failure is never mistaken for venue truth, in particular never for an empty book. A two-member failure type keeps that guarantee exactly: neither member is a `VenueOrderView`, so no call site can read either as "no record", and the type checker refuses the confusion rather than a convention asking politely.

What inv 1 did *not* say, and what this adds, is that a failure carries **why**. The alternative — a flag on the view, or a third `status`-like field — would put a failure inside the successful-read type and leave it one `if` from being read as an empty book, which is the confusion inv 1 exists to make impossible.

Only one caller acts on the distinction. `fetch_account_state` reads a single grain with no worklist behind it, so it collapses both to `None` and keeps its own contract; so does the post-placement fills read. The split is paid for by the one read that drives a list.

## 4. Why the durable case still needs a budget, and why the budget faults

Skipping alone is not the fix. The venue's stored value does not change, so a skipped order is re-read, re-named and re-skipped every cycle for the life of the process: never reconciled again, merely quieter about it. That is fact 1, still standing for every condition ADR-0048 could not name in advance.

ADR-0048 §1 states the obstacle in the taxonomy's own row 2: *"A venue contract change is durable; a truncated or transitional response is not, and nothing here can tell them apart from one sample."* One sample cannot — **several consecutive ones across the cadence can**, and that is the whole of `ReconcileConfig.unreadable_max_attempts` (default 3). Below it, the order is skipped and the pass carries on. At it, waiting has been tried and did not work.

The escalation is a `VenueReadUnresolvable` that faults the engine, and the alternative was considered and rejected:

- **A terminal resolution is a guess.** Nothing in this process read the order's state. `FAILED` or `REJECTED` would mint saga truth out of a body we never parsed, against an order that may be live and filling. No verdict this path can reach is honest.
- **Dropping it from the worklist is the original bug, quieter.** An order the engine holds, never reconciles, and no longer mentions is the permanent silent stall in different clothes.
- **Faulting misstates nothing.** It stops, names the cloid, and hands the condition to the only party that can resolve it — the same verdict and the same reasoning as ADR-0048 §3, whose cost paragraph applies here verbatim: a durably unreadable body kills a running process holding positions, and that is intended.

`VenueReadUnresolvable` is a **sibling** of `VenueFactUnsupported`, not the same type, and the split is on what was known. That one is raised where a fact was read and understood and had nowhere to go, so the venue package names the fact in the refusal. This one is raised where nothing could be read at all, and the only thing anyone knows is that it kept failing. `read` has already named each attempt against that cloid with the body it could not read — key set first — so the refusal points at the evidence rather than re-deriving one line of it.

**The budget resets on any readable read.** The word in "consecutive unreadable bodies" is load-bearing: a venue that garbles one response, answers the next and garbles again is exactly the transitional case a single sample cannot rule out. Without the reset, a long-lived order would accumulate unrelated blips over days and fault a process whose venue was never durably broken. A failed *send* neither advances nor clears the run — an outage is no evidence either way about a body nobody received.

The counter runs per cloid in the engine, beside `ConsecutiveMisses` and the `GhostGate`, because which orders block which is the reconciler's business and not the venue's (ADR-0031). It is deliberately outside ADR-0011 inv 7's timing bound: a skipped order is never counted absent, so this budget cannot race the ghost grace window the way the in-flight retry budget could.

## 5. Why fact 2 is now moot rather than fixed

ADR-0048 §2's fact 2 — `_drive` freezing before `handle` runs, so `_resolve_inflight`'s `ConsecutiveMisses` never advances — reads like a second thing to repair. It is not, and deliberately so.

That budget counts **absent records**: proof that the venue has no record of a cloid, accumulated until a resend-safe `FAILED` is provable (ADR-0008 rule 2). An unreadable body is not an absent record. Advancing the miss count on one would be counting "we could not read the answer" as "the answer was no", which is the exact confusion inv 1 forbids one layer down. The order is skipped, its in-flight budget stands still, and its *own* budget — the one that counts unreadable reads — is what moves. Two ledgers, two units, no interaction.

## 6. Consequences

- **`Exchange.fetch_order` returns `VenueOrderView | VenueReadFailure`.** Both implementations and every double move with it; paper never returns a failure, in-process reads being unable to fail.
- **`reconcile.frozen` gains a `scope` field** (`cycle` / `order`) rather than a second event name — the catalog is closed (ADR-0045) and nothing routes on the difference, but both scopes name the cloid whose read failed, so without it a dashboard cannot tell a pass that stopped dead from a pass that reconciled everything but one order.
- **One `reconcile.frozen` per skipped order, not per pass.** A cycle with three unreadable orders now emits three. Anyone alerting on the count should read `scope`.
- **A durably unreadable body now stops the engine.** Previously it stopped only the reconciler, quietly, and took every order behind it. This is the second entry in what ADR-0048 §6 began: `reconcile.frozen` no longer covers the permanent cases.
- **A startup pass behaves identically to a continuous one**, and inv 5 holds unchanged: skipping still returns `False`, so a pass that could not prove one order never clears the `StartupBarrier`. Whichever escalates first — the budget, or the barrier's own timeout — the engine faults rather than trades on unverified state.
- **#193's `LedgerReconciliation` inherits the grain rather than minting one.** Its "own freeze grain" now has an answer to copy: which failure, how much it costs, and what a durable one does.
