# Where preconditions are stated: write verbs, not constructors

_Recorded during the `/improve-codebase-architecture` review on the [#208](https://github.com/MarcosACH/tickwright/issues/208) branch, which added the non-positive-quantity guard to `Position.apply`. Nothing about the code changes; the ADR states a rule the code has followed since ADR-0002 put saga dedup on `Order.apply` rather than on the event it consumes, and never wrote down — because the review re-derived it from scratch and a future one otherwise will too. Supports **ADR-0014** (which fixes *what* a violated invariant does, not *where* it is checked) and **ADR-0043 §9** / **ADR-0008** (recovery), whose restore paths are the reason the rule cuts the way it does._

All thirty-one `@dataclass` declarations in `src/tickwright/domain/` — twenty-one of them in `events.py`, the rest value objects, specs, and `Order` and `Position` themselves — carry **no `__post_init__`**. `Signal` does not check that its `quantity` is positive; `OrderFillEvent` does not check that `cum_qty >= quantity`; `InstrumentSpec` does not check that `max_decimals >= sz_decimals`. Preconditions are instead stated on the **write verbs** of the three mutable aggregates that consume those values — `Order.apply` refuses an illegal transition against `_LEGAL_TRANSITIONS`; `Position.apply` refuses a misrouted partition and, as of #208, a non-positive fill quantity (`domain/position.py:135-143`); `Account.accrue_realized` states none at all and says so, because the corollary below hands its dedup to `Position.apply`, the verb that owns the key (`Account` is also the one of the three that is not itself a dataclass, which is why only two appear in the census above).

Read cold, that looks like an omission — thirty-one types that "forgot" their validators, the two gatekeepers among them. It is a decision.

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

The repo already shows what the disagreement looks like when it is deliberate. `RealGuard` refuses a size that rounds to zero with a `Denied` verdict (`engine/guard.py:86-89`); `Position.apply` refuses a non-positive fill quantity by faulting the run (#208, [PR #210](https://github.com/MarcosACH/tickwright/pull/210); `domain/position.py:135-143`). Same predicate on the same kind of number, opposite dispositions — and that is *correct*, because they guard different operations at different times: one is a pre-trade business outcome the strategy may recreate, the other is a post-trade impossibility. The rule keeps that distinction legible. A shared constructor validator would have collapsed it into one answer and forced the wrong disposition on one of the two sites.

## 4. Where `__post_init__` *is* right, and what distinguishes it

The two validators in `src/` are both on **configuration** dataclasses — knob bundles chosen once at wiring and never observed in flight:

- `StochasticParams.__post_init__` (`adapters/paper/fill_model.py:45-66`) — the sharpest case, guarding `partial_fill_fraction` against exactly the outcome #208 later guarded at the aggregate. A non-positive fraction offers a zero quantity on *every* crossing tick, so the order never converges. Operator-authored, through `TICKWRIGHT_PAPER__STOCHASTIC__*`.
- The continuous-reconciler config (`engine/reconcile.py:73-97`) — the interval and attempt budgets, and the two *ordering* rules between them: the in-flight retry budget must stay under `ghost_grace_seconds`, and so must the recent-order protection window (ADR-0011 inv 3). Composition-root-authored: `ReconcileConfig` has no environment surface today, and `engine/runner.py:103` falls back to a bare `ReconcileConfig()` when the caller supplies none.

Those two ordering rules are also why this is the config layer's job and not a field-level one: neither is expressible on any single knob, and both are wrong *before the first tick* or not at all.

The distinction is **whether the value is a choice or a report** — not who typed it, and not when the dataclass happens to be constructed. **A choice is validated where it is declared, because the declaration site is the only place the rule exists: there is no later operation that owns it, and a misconfiguration should fail at wiring rather than on the first tick.** A report of external truth is validated where it is consumed, because there the operation that would be corrupted is the one adjudicating.

That declaration site is the **config layer**, and `__post_init__` is only one of its spellings — the one a knob bundle that is a plain dataclass uses. `PaperExchangeConfig.genesis_collateral` states its own floor as a pydantic `Field(gt=0)` — *"an account cannot be created owing money … that is input validation, not margin enforcement"* — and `AppConfig` states the demand that it be present at all, keyed on the `exchange` discriminant (`app/config.py:96`), because that validator is the only scope where both are visible. `AccountSpec`, the domain type the value is then deserialized into, checks nothing.

Authorship is the *detector*, not the rule. An operator (through the environment) and the composition root (in code) both author choices, which is why `StochasticParams` and `ReconcileConfig` sit together despite only one of them being reachable from `.env` — the run never observes either as a fact. An adapter reporting what a venue *is* authors a report, however knob-shaped it looks at the wiring site.

Which is what keeps the **specs** on the carrier side of the rule, though both are also built once at composition. `AccountSpec` is a report of what the venue's account is: **adapter-authored**, not operator-authored — the distinction `CONTEXT.md` already flags under its term (*_Avoid_: account config — it is adapter-authored, not operator-authored*). `InstrumentSpec` is sourced by the venue adapter (ADR-0031): from the meta endpoint on Hyperliquid (`venues/hyperliquid/universe.py:63`), and on paper from `PaperExchangeConfig.instrument_specs` (`adapters/paper/config.py:36`). Only that second half is operator input, so a rule like `max_decimals >= sz_decimals` is statable — but on `PaperExchangeConfig`, where the operator declared it. Not on the dataclass, which the venue-sourced half reaches by a path no config validator sees at all: there the spec is venue truth arriving in flight, and it falls under the general rule, adjudicated where it is consumed by the quantizer and the guard (`engine/guard.py:81-84` already faults on a symbol wired with no spec).

Note that `StochasticParams`'s guard did **not** make #208's redundant, and #208 did not make it removable. They fail at different times — one at composition, one mid-run — and `StochasticParams` guards one fill model on one of the **two** fill producers: a `FillReport` is built at `adapters/paper/exchange.py:264` and at `venues/hyperliquid/exchange.py:335`, and nowhere else. Everything downstream re-publishes what those two produced — the reconciler replays a venue's own fill history through the same `ExecutionManager` seam (`engine/reconcile.py:318-323`) without passing through any fill model at all. Two guards on the same value are acceptable exactly when they are guards on different *events in time*; they are not acceptable as two copies of one check.

## Consequences

- **A missing `__post_init__` in `domain/` is not a defect.** A review that finds one should ask which verb the rule belongs to, not add a validator.
- **A new mutable aggregate states its own preconditions, and a new restore path bypasses them.** Any aggregate joining `Order`, `Position` and `Account` inherits both halves.
- **The rule does not say a precondition is stated only once.** It says each statement must be attached to an operation that would be corrupted without it — so a fill path crossing two aggregates may legitimately state the same rule twice, once per aggregate. That is the open question at the `ExecutionManager` fill path, where `Order.record_fill` (`domain/order.py:199-235`) advances the saga before `PortfolioProjection.apply_fill` refuses; it is left to [#178](https://github.com/MarcosACH/tickwright/issues/178), which brings the first fill producer that can compute a bad quantity.
**(Resolved by [#178](https://github.com/MarcosACH/tickwright/issues/178): the fill path states the rule twice, once per aggregate.** `Order.record_fill` now refuses a non-positive quantity ahead of every mutation it makes, alongside the `Position.apply` check #208 put there.

The deciding argument was not symmetry but that each aggregate is corrupted *differently*, so neither statement stands in for the other. On the ledger side a zero takes the flat branch and leaves a partition flat at a non-zero entry price. On the saga side it accumulates into `cum_qty`, which selects the terminal — and it **spends the venue's `trade_id`**. That second effect is the one containment did not cover: refusing only at the ledger burns the id against a fill nobody booked, so the trade coming back, corrected or merely redelivered, dedups as an echo and is dropped silently. The saga is the aggregate the path reaches first, so it is the first verb that cannot proceed without the rule, per §1's corollary.

`Position.apply` keeps its own check for the reason the two are not one: the reconciler's synthetics reach it with no saga in front of them at all (ADR-0034), so a rule stated only on `record_fill` would not cover the producer this issue introduced.**)**

- **Type-level enforcement is not foreclosed.** Nothing here argues against expressing a precondition in the type system where one fits; the decision is about *runtime* validators and where they attach.
