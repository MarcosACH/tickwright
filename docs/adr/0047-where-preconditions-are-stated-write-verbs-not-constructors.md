# Where preconditions are stated: write verbs, not constructors

_Recorded during the `/improve-codebase-architecture` review on the [#208](https://github.com/MarcosACH/tickwright/issues/208) branch, which added the non-positive-quantity guard to `Position.apply`. Nothing about the code changes; the ADR states a rule the code has followed since ADR-0002 and never wrote down, because the review re-derived it from scratch and a future one otherwise will too. Supports **ADR-0014** (which fixes *what* a violated invariant does, not *where* it is checked) and **ADR-0043 §9** / **ADR-0008** (recovery), whose restore paths are the reason the rule cuts the way it does._

All thirty-one `@dataclass` declarations in `src/tickwright/domain/` — twenty-one of them in `events.py`, the rest value objects and specs — carry **no `__post_init__`**. `Signal` does not check that its `quantity` is positive; `OrderFillEvent` does not check that `cum_qty >= quantity`; `InstrumentSpec` does not check that `max_decimals >= sz_decimals`. Preconditions are instead stated on the **write verbs** of the three mutable aggregates that consume those values — `Order.apply` refuses an illegal transition against `_LEGAL_TRANSITIONS`; `Position.apply` refuses a misrouted partition, and gains a non-positive-quantity refusal with #208.

Read cold, that looks like an omission — twenty types that "forgot" their validators. It is a decision.

## 1. Decision

**A precondition is stated on the operation that would be corrupted by violating it, never on the constructor of the value that carries it.** Domain events, value objects and specs are inert carriers and validate nothing. The aggregate's write verb is the gatekeeper, and it raises `InvariantViolation` (ADR-0014).

The corollary that gives the rule its teeth: **a well-formedness rule belongs to whichever verb is the first one that cannot proceed without it.** #208 put "a fill moves a positive quantity" on `Position.apply` because average-cost accounting is what a zero breaks — not on `OrderFillEvent`, where it would have been a rule about a shape rather than about an operation.

## 2. Why not the constructor: recovery rebuilds state that no write verb would produce

This is the load-bearing reason, and it is invisible from the type definitions.

Every persisted aggregate is restored by writing its fields **directly**, deliberately bypassing the verb that validates them:

- `Order.restore` (`domain/order.py:124-169`) sets `state` from the checkpointed row without consulting `_LEGAL_TRANSITIONS`. It *must*: the row is the outcome of transitions already adjudicated before the crash, and re-adjudicating a terminal state as if it were arriving fresh would refuse to recover a correctly-recorded saga.
- `Account.restore` (`domain/account.py:114-137`) sets `_cash` directly rather than replaying accruals, because the store holds the settled line, not the fills that moved it.
- `restore_position` (`adapters/store/_records.py:248-257`) rebuilds a `Position` field-by-field through the raw constructor.

If validation lived at construction, the recovery path would have to satisfy **runtime preconditions while restoring historical state** — two different jobs, and the second is not a weaker version of the first. A restored `Position` legitimately holds a signed size no single fill produced; a restored `Order` legitimately sits in a state no legal transition from `INITIALIZED` reaches in one step. Constructor validators would either reject those (breaking recovery) or be weakened until they check nothing worth checking.

Stating the rule on the write verb keeps the two paths honestly separate: **`apply` guards the future, `restore` reproduces the past.**

## 3. Why not both: a check in two places is a check that can disagree

The alternative worth naming is defence in depth — validate at construction *and* at the verb. Rejected, on the same grounds ADR-0045 §1 rejected a second channel for a fact the bus already carries: two statements of one rule drift, and the failure is silent, because the weaker of the two is the one that governs.

The repo already shows what the disagreement looks like when it is deliberate. `RealGuard` refuses a size that rounds to zero with a `Denied` verdict (`engine/guard.py:86-89`); `Position.apply` refuses a non-positive fill quantity by faulting the run (#208, landing with [PR #210](https://github.com/MarcosACH/tickwright/pull/210)). Same predicate on the same kind of number, opposite dispositions — and that is *correct*, because they guard different operations at different times: one is a pre-trade business outcome the strategy may recreate, the other is a post-trade impossibility. The rule keeps that distinction legible. A shared constructor validator would have collapsed it into one answer and forced the wrong disposition on one of the two sites.

## 4. Where `__post_init__` *is* right, and what distinguishes it

The two validators in `src/` are both on **configuration** dataclasses, set once at wiring and never in flight:

- `StochasticParams.__post_init__` (`adapters/paper/fill_model.py:45-62`) — the sharpest case, guarding `partial_fill_fraction` against exactly the outcome #208 later guarded at the aggregate. A non-positive fraction offers a zero quantity on *every* crossing tick, so the order never converges.
- The continuous-reconciler config (`engine/reconcile.py:73-85`) — interval and attempt budgets.

The distinction: **a knob is validated where it is declared, because there is no later operation to attach the rule to and a misconfiguration should fail at wiring rather than on the first tick.** A fact in flight is validated where it is consumed, because there is one.

Note that `StochasticParams`'s guard did **not** make #208's redundant, and #208 did not make it removable. They fail at different times — one at composition, one mid-run — and the paper path is only one of three fill producers. Two guards on the same value are acceptable exactly when they are guards on different *events in time*; they are not acceptable as two copies of one check.

## Consequences

- **A missing `__post_init__` in `domain/` is not a defect.** A review that finds one should ask which verb the rule belongs to, not add a validator.
- **A new mutable aggregate states its own preconditions, and a new restore path bypasses them.** Any aggregate joining `Order`, `Position` and `Account` inherits both halves.
- **The rule does not say a precondition is stated only once.** It says each statement must be attached to an operation that would be corrupted without it — so a fill path crossing two aggregates may legitimately state the same rule twice, once per aggregate. That is the open question at the `ExecutionManager` fill path, where `Order.record_fill` (`domain/order.py:199-235`) advances the saga before `PortfolioProjection.apply_fill` refuses; it is left to [#178](https://github.com/MarcosACH/tickwright/issues/178), which brings the first fill producer that can compute a bad quantity.
- **Type-level enforcement is not foreclosed.** Nothing here argues against expressing a precondition in the type system where one fits; the decision is about *runtime* validators and where they attach.
