# Paper account genesis: a required starting collateral, and a store that fail-fasts when the genesis changes

_Accepted via the D9 grilling session on decision ticket [#136](https://github.com/MarcosACH/tickwright/issues/136), part of the trade-economics map [#107](https://github.com/MarcosACH/tickwright/issues/107). Supplies the `genesis_collateral` that ADR-0040 §7's equity formula takes as given, and the `paper-<label>` identity that ADR-0038 sourced from `PaperExchangeConfig` without saying how. **Amends ADR-0040 §7 and ADR-0041 §4.1/§7** (a zero genesis is no longer reachable *through config* — it survives on the live path, ADR-0042 §6) and **extends ADR-0038's store fail-fast** from `account_id` to the account's whole opening declaration. Constrains the durability ticket ([#137](https://github.com/MarcosACH/tickwright/issues/137)): the genesis column, the merged mismatch error, and the closed write-set are this ADR's, the schema and cadence are that one's._

The live account's opening state is ingested — Hyperliquid reports what is in the wallet. The paper account has no venue to ask, so its cash line needs a genesis value: the one number the whole Tier-1 ledger (equity, free margin, effective leverage) is measured against. This ADR fixes where that number comes from, what happens when it changes under a ledger that already exists, and what closes the set of things allowed to move the cash line afterwards.

## 1. Required, strictly positive, and demanded only when the paper venue is selected

`PaperExchangeConfig` gains `genesis_collateral: Decimal` with **no default** and a `gt=0` constraint. There is no out-of-the-box paper collateral: an operator states the number or the process does not start.

**One name for one number.** The config field, the persisted column, ADR-0040 §7's `equity = genesis_collateral + …` formula and CONTEXT.md's **Genesis collateral** term all read `genesis_collateral`. An `initial_collateral` knob feeding a `genesis_collateral` column would be two names for one value across a cross-check (§3) whose entire job is comparing them — and CONTEXT's own *Avoid* list already rules out "initial deposit" for this number, on the grounds that nothing is deposited.

A default was rejected in both directions, and the rejection is the decision. A **non-zero default** (say `100_000`) makes the first run look plausible while reporting equity, margin and effective leverage against capital nobody chose — silent fiction, and precisely the fabrication ADR-0039 and ADR-0041 refuse everywhere else in this surface. A **zero default** is honest but ships a first-run experience where the first fill's fee drives cash negative and free margin stays negative forever; the numbers are correct and read as broken. Neither failure is worth avoiding a one-line config entry.

**A `.env.example` seed is not a default, and the distinction is the whole point.** The canonical reference carries a concrete `TICKWRIGHT_PAPER__GENESIS_COLLATERAL`, because the file's contract is that `cp .env.example .env` yields a runnable config — a commented-out required variable would make the documented quickstart fail-fast on its first command. That is not the fabrication rejected above. A code default is invisible, applies to every operator who never thought about it, and cannot be seen in any file they own; a template seed lands in the operator's own `.env`, on a line they copied deliberately, under a comment saying it is required and has no default. The value is *theirs to edit*, and an operator who deletes the line gets the fail-fast this section demands. What is refused is a number the engine supplies when nobody chose one — not a number the reference shows them how to choose.

**Strict positivity, not `ge=0`.** A non-positive starting collateral is a typo, not a scenario — an account cannot be *created* owing money. Rejecting it is **not** margin enforcement, which this map rules out (`#107`): that ceiling governs *derived* state, and derived free margin still goes negative freely and without consequence (ADR-0040 §7). Refusing nonsense *input* at config validation is a different act from refusing a trade at runtime.

**The demand is conditional on selection, not on import.** `AppConfig` holds the block as `paper: PaperExchangeConfig | None = None` and raises only when `exchange == "paper"`, reusing verbatim the pattern `ReplayFeedConfig` and `feed == "replay"` already establish in `app/config.py`. This preserves a property a plain required field would destroy: **a live-exchange run is never forced to invent a paper number.** `config.paper` is read in exactly one place — the `case "paper":` arm of `build.py` — so nothing else needs narrowing.

## 2. One `Decimal`; USDC stays implicit

Collateral is a bare `Decimal`. No currency travels with it, and **`AccountSpec` does not gain the collateral-currency field ADR-0038 anticipated** — that additive path stays unspent.

This is consistent with every money decision already made: ADR-0029 makes all money `Decimal`, ADR-0036 makes the per-fill fee a bare signed `Decimal` with USDC implicit, and ADR-0041 gives the read API no `currency` argument. Hyperliquid perps are USDC-settled only and spot is deferred with the instrument itself (ADR-0030).

**Rejected: model the currency now.** The cost of a genuine multi-currency account is not one field — it is a dimension. Every aggregate becomes a per-currency map (`equity`, `free_margin`, `realized_pnl`), an FX-rate layer joins the model, and a third result state appears beside `Decimal` and `None`: *conversion unavailable*, when no rate exists to combine two native buckets. ADR-0041's `AccountView` would carry that shape for a venue with exactly one collateral currency.

**Rejected: declare it without modelling it** (`collateral_currency: str = "USDC"` on `AccountSpec`, purely so ADR-0036's live-ingress `feeToken` check reads against a spec rather than a literal). It changes no behavior, and a field with exactly one possible value is a seam with one implementation — which the repo's own standard calls hardcoded. The field earns its place when a second collateral currency is real.

**Trigger for revisiting:** a venue or instrument class settling in anything other than USDC — spot (ADR-0030), or a second venue adapter. At that point `AccountSpec` gains the field, and this section is what says why it wasn't there first.

## 3. Genesis is a creation seed; the store is authority; a changed genesis fail-fasts

The configured value is used **once**, to seed the account row when it does not exist. It is persisted as its own column — distinct from the cash line, which accumulates away from it — and on every subsequent start it serves exactly one purpose: a **cross-check**.

- **The cash line is restored from the `Store`,** never recomputed from config. ADR-0034 makes the store system-of-record for Tier-1, and cash is Tier-1.
- **Config genesis ≠ persisted genesis → refuse to start,** naming both values and the remedy.

**Rejected: config re-applies as a rebase** (persist only the flows, recompute `cash = configured_genesis + Σ flows` each start). Editing the value would shift equity discontinuously across a restart with no event recording it, making any PnL series spanning that restart meaningless — and it contradicts ADR-0034 outright by making config, not the store, the Tier-1 authority.

**Rejected: config inert after creation** (seed once, ignore forever, no check). Never blocks a restart, but turns an edit into a **silent no-op**: the operator sets a new number, the surface keeps reporting the old ledger, and nothing in the logs explains the discrepancy. That trades a loud failure for a quiet one.

**The mismatch error is one error, not several.** Changing the label trips ADR-0038's `account_id` check; changing the collateral trips this one. Both mean *this store belongs to a different account history*, so they are one failure reporting **every** mismatching field at once — an operator who changed both learns both on the first restart, not one per restart. The remedy is always the same and is always stated: point the store path at a fresh location, or restore the declared values.

```
StoreAccountMismatch: the ledger in <store path> belongs to a different account.
  account_id:         stored 'paper-main'  config declares 'paper-momentum_v2'
  genesis_collateral: stored 100000        config declares 200000
A different genesis is a different account history. Point the store at a fresh
path to open a new ledger, or restore the declared values to resume this one.
```

The escape hatch needs no new machinery: the store path is already config (`SQLiteStoreConfig`), so "I want a different genesis" and "I want a different account" are the same operator action, expressed the same way.

## 4. The cash line has exactly four accretive inputs

**Genesis, realized PnL, fees, funding.** Nothing else may *add to* the cash line. Deposits, withdrawals and transfers are **not modelled** — not on paper, where the account is funded once at creation, and not on live.

The value of stating this is not the feature avoided; it is that a **closed** write-set makes the Σ-invariant checkable and gives #137 a finite list of writers to enforce.

```
cash = genesis_collateral
     + Σ realized_pnl        (ADR-0034)
     + Σ fees                (signed; maker rebate < 0 — ADR-0036)
     + Σ funding             (ADR-0037)
```

**Accretive inputs, not every writer — the reconcile heal is the standing exception.** ADR-0034 already gives the live path one more way for `cash` to move: on a Tier-1 divergence the reconciler emits a synthetic adjustment event — a reconciliation fill *and/or a cash adjustment* — through the same idempotent `apply()`. That is a **correction toward venue truth**, not a fifth input: it does not accrue from anything the engine did, it exists precisely because the four inputs above failed to reproduce the venue's number, and it is **live-only** (paper has nothing to reconcile against). The Σ-invariant is the thing being checked *by* the heal, not something the heal violates. So #137 enforces "these four accrue" over a ledger that ADR-0034's synthetic adjustment may still correct — an enforcement written to forbid the heal would contradict ADR-0034's non-negotiable venue-authoritative rule.

**A real deposit on live is a known benign alert.** An operator funding the wallet mid-run moves `accountValue`, which the next reconcile sees as a Tier-1 cash divergence. Under ADR-0034 it heals to venue truth and alerts. The heal is correct — the venue is authoritative — but the alert fires for a legitimate action rather than a defect. This is recorded here so a future reader diagnosing that alert stops at "expected" instead of hunting a ledger bug.

**Rejected: a first-class, operator-facing `CashAdjustment` event** mirroring ADR-0037's `FundingAccrual` — keyed, idempotent, its own Tier-1 line, injectable on paper and ingested on live. It is coherent and would let a paper run simulate a mid-run top-up, but it adds an event type, an idempotency key, a durable line and a config surface to a map whose destination is *reporting* a perp trade's economics. **This rejects the deliberate cash movement, not the reconciler's synthetic cash adjustment above** — ADR-0034's heal is untouched and stays exactly what it was.

**Rejected: ingesting the venue ledger purely to annotate divergences** (Hyperliquid exposes `userNonFundingLedgerUpdates`, and the pinned SDK exposes `user_non_funding_ledger_updates` — R2 #109). It would remove the false-positive alert class above by attributing a cash divergence to its deposit, but it costs a new live read path, a new dedupe key and unresearched endpoint semantics — most of the cost of modelling cash movements, for none of the capability. The endpoint is named here as the door, if it is ever worth opening.

## 5. The account label: defaulted, slug-constrained, never ambient

`PaperExchangeConfig` gains `account_label: str = "default"`, from which the adapter composes ADR-0038's qualified `paper-<label>`. Constrained to lowercase alphanumerics and underscores, 1–32 characters, **no hyphen**.

**Why it may default when the collateral may not.** A wrong label has no silent economic consequence. It cannot make a number wrong; it can only point at the wrong ledger, and §3's fail-fast catches exactly that, loudly. The collateral has no such backstop — a wrong number reports as a right one.

**Why no hyphen.** The qualified ids are asymmetric by construction: paper is two segments (`paper-main`), live is three (`hyperliquid-testnet-0xABC…`, venue + network + identifier). An unconstrained label collapses that distinction — `paper-testnet-foo` reads as a live-shaped id — and any consumer that ever splits an id on hyphens for grouping or display gets a silently wrong answer rather than an error. One character class buys ids that stay unambiguous to a human and to a parser.

**Never derived from anything ambient** — not hostname, pid, timestamp, or user. ADR-0012's reproducibility guarantee requires that two runs of the same config produce the same account identity; a derived label breaks that, and would also let the same store be opened under a different id from a different machine.

Collision is not a correctness concern: two paper ledgers may both be `paper-default` in different stores, because the id is checked **against the store that holds it** (§3), never against a global registry. The cost is only that telemetry cannot tell them apart, which is what a label is for.

## 6. The live path configures nothing; its genesis is ingested

`HyperliquidConfig` gains no collateral field, and that absence is deliberate rather than an omission. The live account's opening state is read from `clearinghouseState` — equity is `marginSummary.accountValue`, free margin is root `withdrawable` (R1 #108). Configuring it would invent a number the venue already knows, contradict ADR-0034's venue-is-authoritative rule for Tier-1, and fail-fast on every legitimate deposit — the case §4 just classified as benign.

The genesis **column** is nonetheless populated on live, so it stays `NOT NULL` on both paths and "when did this ledger open, and at what value" stays answerable. The account row is created at the first reconcile with:

```
genesis_collateral = accountValue − Σ unrealized_pnl
```

**The subtraction is load-bearing and is required regardless of the column.** `accountValue` is *equity* — it already contains unrealized PnL. Writing it into the cash line would double-count uPnL the instant `equity = cash + Σ uPnL` (ADR-0040 §7) is evaluated: an account opened holding a position would report its uPnL twice from the first cycle. Tier-1 opening cash therefore needs this identity whether or not genesis is separately recorded; recording it is free once the subtraction exists.

On live the value is **provenance only** — nothing cross-checks it, because there is no configured counterpart to check against. §3's fail-fast is inherently paper-only; ADR-0038's `account_id` check applies on both paths.

**`gt=0` is a validator on the paper config input, never a constraint on the column.** The two are easy to conflate and must not be: an unfunded wallet opens with `accountValue = Σ uPnL = 0`, so the live-ingested genesis is legitimately `0`, and a `CHECK (genesis_collateral > 0)` in #137's schema would reject the exact row §6 tells it to write. The column's constraint is `NOT NULL` and nothing more. §1's "an account cannot be *created* owing money" is an argument about what an operator may **declare**, not about what a venue may **report**.

## Consequences

- **ADR-0040 §7 is amended.** Its statement that *"A zero genesis drives it negative on the first trade; paper still runs"* describes a state `gt=0` makes unreachable through config. The substantive rule it belongs to is untouched: free margin still goes negative, and is still reported without rejection, liquidation or alert.
- **ADR-0038's collateral-currency note is answered, not deleted** — deferred with a stated trigger (§2), and its store fail-fast is extended from `account_id` to the account's full opening declaration, merged into one error (§3).
- **ADR-0041's zero-genesis lifecycle case is re-based, not removed.** Its §4.1 and §7 cited a *configured* zero genesis as the worked example of a `None` `effective_leverage` before the first fill; `gt=0` makes that unreachable on paper, so the example moves to the live path's ingested `0` (§6). The seam behavior — non-positive denominator reads `None` — is untouched.
- **ADR-0035's `PortfolioProjection` gains a closed write-set** for the cash line (§4) — four *accretive* inputs, enumerable and enforceable, over a ledger ADR-0034's reconcile heal may still correct on live.
- **#137 inherits three hard constraints**: a `NOT NULL` genesis column populated on both paths (`NOT NULL` only — positivity is a paper-config validator, §6), a merged `StoreAccountMismatch` raised before any recovery work begins, and the four-accretive-input write-set. The schema, the write cadence and the restart ordering remain that ticket's to decide.
- **A first paper run now requires one config line.** `TICKWRIGHT_PAPER__GENESIS_COLLATERAL` joins the paper path's setup; `.env.example` carries it as the canonical reference — as a seed the operator edits, not a default (§1). The hermetic test suite is unaffected in kind — tests build `AppConfig` directly and simply pass the field — but every existing paper-path fixture gains it.
- **Changing a paper account's collateral or label now requires an operator decision**, not an edit: either open a fresh ledger at a new store path, or keep the declared values. This is the intended friction — the alternative is a ledger whose history means something different from what its config says.
