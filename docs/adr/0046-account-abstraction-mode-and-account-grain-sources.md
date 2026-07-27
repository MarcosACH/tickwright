# Supported account abstraction mode: Manual/Standard only, and where account-grain equity and free margin are read

_Accepted via the D13 grilling session on decision ticket [#148](https://github.com/MarcosACH/tickwright/issues/148), graduated from the [#142](https://github.com/MarcosACH/tickwright/issues/142) testnet validation task on the trade-economics map [#107](https://github.com/MarcosACH/tickwright/issues/107). Fixes the premise every account-grain decision in this surface rests on: **that `clearinghouseState` reports the account's equity**. It does — under exactly one family of venue account modes, which this ADR makes a deployment precondition and verifies at boot. **Amends ADR-0034** (the anchor's field sources), **ADR-0040** (§2's `free_margin` source, §3's liquidation `null` rule and its paper mirror, §6's alert-band reference — resolving the defect §6 carries a `#148` pointer for), **ADR-0044** (the boot step ordering), and **ADR-0024** (startup step 4 opens with the mode gate, and the barrier-failure policy gains a third consumer). **ADR-0042 and ADR-0043 stand decisionally unchanged**, with the precondition their formulas depend on now named — ADR-0042 §6 additionally takes an incidental in-place correction, its aside naming free margin as root `withdrawable` being superseded by §2 here._

Hyperliquid lets an account choose how its spot and perps balances interact. The choice is
invisible in every field this surface reads, and it silently changes what those fields **mean**.
[#142](https://github.com/MarcosACH/tickwright/issues/142) measured the consequence on a funded
testnet account: with a live cross position open, `clearinghouseState.marginSummary.accountValue`
read **25.9264** while the account held ≈**977.58** USDC, and `withdrawable` read **0.0096** against
≈**951.66** actually available.

Nothing was wrong with the arithmetic. The account was in `unifiedAccount` mode, where the perps
clearinghouse is a **sub-ledger** reporting only the collateral posted into perps — and where the
venue's own documentation says *"unified account and portfolio margin show all balances and holds
in the spot clearinghouse state. Individual perp dex user states are not meaningful."*

ADR-0034 anchors reconciliation on that account summary. ADR-0040 §2 sources `equity` and
`free_margin` from it. ADR-0042 derives the live genesis cash line from it. ADR-0043 materialises
the account from it at boot. Under the mode the venue calls *"recommended for most users"*, all four
read a number that is not the account's equity.

## 1. Supported modes: Manual/Standard only, as a deployment precondition

**Decision: Tickwright supports exactly one account abstraction family — Manual/Standard — and
refuses to start against anything else.**

The venue exposes four modes. Their properties, taken from the venue's
[account-abstraction-modes page](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/account-abstraction-modes):

| mode | what it does | who it is for |
|---|---|---|
| **Unified Account** | one balance per asset, unified across spot and perps | *"recommended for most users"* |
| **Portfolio Margin** | one portfolio unifying HYPE, BTC, USDC, USDT | *"most capital efficient"* |
| **Manual/Standard** | separate perp and spot balances, separate DEX balances | *"recommended for market makers, high volume automated users, and deployers"* |
| **DEX Abstraction** | legacy USDC-to-perps routing | being discontinued |

The reasoning is three-layered, and the first layer is decisive on its own:

- **The account is already a deployment fact.** ADR-0038 fixed one process = one venue = one
  account. The abstraction mode is a property *of that account*, so it is the same kind of fact,
  and this map's established answer to a deployment fact is a documented precondition plus a
  startup refusal — `StoreAccountMismatch` (ADR-0042/0043) and ADR-0044 §5's held-leverage refusal
  are the two existing instances. This is the third, and it is the cheapest correct answer.
- **Under Manual/Standard the perps clearinghouse *is* the account boundary**, and that boundary is
  exactly this surface's domain. The map's scope is Hyperliquid **perpetuals only**; spot economics
  are deferred with the spot instrument itself (ADR-0030). Standard mode gives us a walled trading
  account whose cash line no spot activity can touch. Unified deliberately dissolves that wall —
  which is precisely why the anchor breaks under it.
- **The venue recommends it for us in writing.** The mode row above says Manual/Standard is for
  *"high volume automated users"*. It is also the only mode with **no daily action cap**: unified
  and portfolio margin are limited to 50 000 user actions per day, a constraint ADR-0044 §1's
  action-budget reasoning would otherwise have to absorb.

**Consequence, stated plainly: this is real friction for anyone else adopting Tickwright.** Sampling
170 randomly-chosen addresses that traded on mainnet the day this was decided returned a mode for
**114** of them: **93 `unifiedAccount`, 7 `portfolioMargin`, 9 `disabled`, 5 `default`** — so
roughly **88 % of the classified sample** (100 of 114) is on a mode Tickwright refuses. The
remaining 56 addresses are **unclassified, and the reason was not recorded** — the ratio is
therefore reported over the 114 that answered, and it is stated here rather than left implicit so
the denominator is not later mistaken for the sample size. The refusal is designed as a remediation,
not a complaint (§3).

### Alternatives rejected

- **Support Manual/Standard *and* Unified** (the "two implementations per seam" allowance). Buys the
  venue's popular mode; costs a second account-grain sourcing path whose formulas are **unmeasured**,
  and structurally couples the ledger's cash line to **spot activity** — under unified, a spot swap,
  an airdrop or a token purchase all move the same USDC balance the reconciler compares against, so
  each would land as a Tier-1 cash divergence that heals and alerts. ADR-0042 §4 accepted that
  behaviour for the *rare* unmodelled deposit; unified makes it routine. Named as an extension point
  with a stated trigger below.
- **Mode-blind: read both clearinghouse states and merge.** Rejected on two grounds. It requires a
  **multi-currency balance model** — per-currency `total`/`locked`/`free` plus per-instrument
  margins — which this surface deliberately does not have (ADR-0029/0042 §2: one `Decimal`, USDC
  implicit), so it re-opens the collateral-currency dimension ADR-0042 left unspent. And the merge
  cannot be done safely from the obvious fields: #142 measured `totalRawUsd = −103.66` on a **long**,
  establishing that it is the cash leg *net of cost basis*, not cash — so any merge keyed on it is
  reading a quantity that does not mean what its name suggests.
- **Support Portfolio Margin.** Out of scope by construction: it collateralizes perps with HYPE, BTC
  and USDT as well as USDC, which is the multi-currency model plus haircuts plus an FX layer.

**The extension trigger:** if a deployment genuinely needs spot and perps to share collateral, that
is the same trigger ADR-0042 §2 named for the collateral-currency field — and it should be taken as
one piece of work, not as a second sourcing path bolted onto this one.

## 2. What the anchor reads

**Decision: `equity` ← `marginSummary.accountValue`; `free_margin` ←
`crossMarginSummary.accountValue − crossMarginSummary.totalMarginUsed`; `withdrawable` is not read;
`spotClearinghouseState` is never read.**

The equity source is unchanged from ADR-0034 and is correct under Manual/Standard:
`marginSummary.accountValue` is total account equity including isolated positions, which is exactly
this surface's `cash + Σ unrealized_pnl` (ADR-0045).

**The free-margin source changes, and the field it changes away from was wrong.** R1
([#108](https://github.com/MarcosACH/tickwright/issues/108)) recorded *"free = root `withdrawable`"*,
and #142 confirmed `available = accountValue − totalMarginUsed` exactly. Both are right only when
nothing is resting on the venue. Measured on two mainnet standard-mode accounts:

| account | `crossAV − crossTMU` | `withdrawable` | gap | resting-order margin |
|---|---|---|---|---|
| `0x48cd…4955` — 2 cross positions, 1 resting order | 23564.614922 | 23538.934922 | **25.680000** | 128.40 notional ÷ 5x = **25.68** |
| `0x863a…fd6f` — 1 isolated position, 23 resting orders | 24518.081440 | 0.245190 | **24517.83625** | 49035.6725 ÷ 2x = **24517.83625** |

Exact to the last decimal, both. So:

> **`withdrawable` deducts the initial margin reserved by resting orders.** It answers *"how much
> could I take off the venue right now"* — a different question from *"how much free collateral does
> this account have"*.

This is **not** a tolerance problem and no alert band can absorb it. Order margin is exactly what
this surface deliberately does not model — the map's ceiling forbids margin-gated admission, and
ADR-0045 dropped the word "initial" from `margin_used` for precisely that reason. Worse, ADR-0024
leaves resting `LIVE` orders on the venue across a graceful stop, so **having resting orders is the
normal state of a running engine**: the gap would be present almost always, and unbounded.

**ADR-0040 §2's formula itself is confirmed correct.** Checked against both accounts above,
`equity − Σ margin_used` reproduces `crossMarginSummary.accountValue −
crossMarginSummary.totalMarginUsed` exactly, *including the isolated case*: the isolated collateral
carried inside `cash` and the isolated `margin_used = collateral + unrealized_pnl` (ADR-0040 §3, as
corrected by #142) cancel, leaving exactly the cross pool. That independently confirms ADR-0040 §7's
*"isolated collateral buckets are locked and excluded from free"*.

**Recorded honestly:** of three standard-mode accounts with **no** resting orders of any kind, two
matched `crossAV − crossTMU` exactly and one (`0x6de6…ef7b`, 50 cross positions) carried an
unattributed residual of **271.072769** — not explained by open orders, and not by the venue's
`max(initial_margin, 0.1 × total_position_value)` transfer rule, which does not bind there. So
`withdrawable` carries at least one term beyond order margin that remains unidentified. That
**strengthens** this decision rather than qualifying it: it is one more reason the field is not a
cross-check source. Chasing the residual is carried by the re-validation task graduated below.

## 3. The boot check: an allowlist of two literals, gating the venue writes

**Decision: one unsigned `userAbstraction` info read in `HyperliquidExchange.start()`, at ADR-0024
step 4, ordered *before* ADR-0044's leverage alignment. The allowlist is `{"default",
"disabled"}`. Anything else refuses to start.**

**The obvious check is wrong, and it fails on exactly the account this ADR prescribes.** The mode
literal set is four values, read live from the venue:

| literal | meaning | reached how |
|---|---|---|
| `"default"` | Manual/Standard | never explicitly set |
| `"disabled"` | Manual/Standard | **`userSetAbstraction("disabled")`** |
| `"unifiedAccount"` | Unified | `userSetAbstraction("unifiedAccount")` |
| `"portfolioMargin"` | Portfolio margin | `userSetAbstraction("portfolioMargin")` |

The pinned SDK's type carries only three — `Abstraction = Literal["unifiedAccount",
"portfolioMargin", "disabled"]` — because `"default"` is the *unset* state and appears only on the
read. **The remediation this ADR prescribes moves an account from `"unifiedAccount"` to
`"disabled"`, not to `"default"`**, so a `mode == "default"` check would refuse the very account the
operator was told to produce. Both literals were verified to behave identically as standard:
`0x007d…a23d` (`"default"`) and `0xb629…9cc8` (`"disabled"`) both run real perp positions with the
perps clearinghouse fully populated and spot empty.

- **Allowlist, not denylist.** A literal the venue adds later refuses. A mode we have never seen is
  the worst possible case in which to guess.
- **Ordered before the leverage push, gating it.** A wrong mode invalidates the premise ADR-0044's
  check reasons from, so reporting leverage mismatches computed against a margin model that does not
  apply would be noise on top of an error. Gate first, then check. Nothing else touches the venue
  until the gate passes.
- **Its own error type — `VenueAccountModeUnsupported`, not merged into `StoreAccountMismatch`.** An
  `InvariantViolation` (ADR-0014's second error class → `FAULTED`), named beside
  `StoreAccountMismatch` (ADR-0042) and `VenueLeverageMismatch` (ADR-0044 §5) exactly as those two
  are named beside each other. ADR-0042 merged the two *store* checks because they are the same kind
  of disagreement discovered at the same moment; this one is categorically different, and its name
  says so — it is **not** a `*Mismatch`, because it does not disagree with a recorded value. It
  invalidates the meaning of every value read afterwards.
- **A failed read refuses to start** — bounded retry under `startup_reconciliation_timeout`, then
  `FAULTED`, reusing ADR-0024's barrier budget exactly as ADR-0044 §6 does. Never "assume standard on
  error": that is ADR-0011 invariant 1's freeze-never-fabricate rule applied to a precondition. An
  **unrecognised** literal is not a failed read — it takes the allowlist's refusal above.
- **The error is a remediation.** Given that ~88 % of the sampled accounts that answered are on a
  refused mode (§1), it names the observed mode, the two accepted literals, and the fix:
  `userSetAbstraction("disabled")`
  with the master wallet, then a spot→perps `usdClassTransfer`. Both are **user-signed** actions an
  agent wallet cannot perform, which is why this is an operator step and not something the engine
  can do for itself.
- **Paper is a no-op.** The mode is a live-only concept, and no live run may read a paper block or
  vice versa (ADR-0042 §1).
- **No configuration surface.** The mode is read from the venue, never declared. `.env.example` gains
  nothing.

## 4. Re-verification in flight: divergence-triggered, freeze on change

**Decision: read the mode at boot, and re-read it before applying any Tier-1 **account cash** heal.
On a changed mode — or on a re-read that fails or returns an unrecognised literal — refuse the heal,
freeze the account-grain reconcile, and emit `ACCOUNT_MODE_UNVERIFIED`. Never `FAULTED`.**

The hole a boot-only check leaves is not that the cross-check goes stale — it is that **ADR-0034
heals Tier-1 toward the venue**. A mode switch mid-run would write the perps sub-ledger's smaller
`accountValue` into `cash` through a synthetic cash adjustment. It alerts, but it also corrupts, and
ADR-0043 persists the result.

Three shapes were weighed:

- **Boot only** — ADR-0044's precedent (*config wins at startup, the venue wins in-flight*), zero
  steady-state cost. Rejected because ADR-0044 chose that for a **write** with a real downside to
  repeating; here the hole stays open and the thing falling through it writes to disk.
- **Every accounting-reconcile cycle** — closes it flatly, at 2 reads/min × weight 20 = **40 of the
  1200 weight/min IP budget (3.3 %)** on the ~30 s cadence (ADR-0011). Rejected as a poor trade:
  `userAbstraction` is **weight 20** against `clearinghouseState`'s **weight 2**, so this spends 10×
  on the guard what it spends on the guarded read, forever, to catch an event that requires a
  deliberate master-wallet action.
- **Divergence-triggered (chosen)** — costs nothing in steady state and puts the check on the code
  path that does the damage rather than on a timer hoping to reach it first. It needs **no threshold
  to tune**: *any* account-cash divergence triggers the re-read, including the legitimate ones (an
  unmodelled deposit, ADR-0042 §4), where it simply confirms the mode is unchanged and heals
  normally. A mode switch producing no cash divergence is empty as a concern — the switch *is* a
  re-pooling of balances.

**Freeze, not fault.** The local Tier-1 ledger remains correct (our fills are still our fills) and
every Tier-2 number is computed from `(position, mark)`, never from the venue (ADR-0034/0039) — so
only the cross-check and the heal become invalid. Freezing the account-grain reconcile is ADR-0011
invariant 1's existing mechanism applied to a **semantic** outage rather than a network one. The
engine keeps trading on numbers that are still right, and stops trusting a snapshot that no longer
means what it did.

**An unreadable mode takes the same branch as a changed one — the guard fails closed.** A re-read
that errors, times out, or returns a literal outside the allowlist leaves the mode **unverified**,
and an unverified mode is not evidence that it is unchanged. Proceeding would heal on exactly the
assumption this section exists to stop the engine making, so the heal is refused and the
account-grain reconcile freezes identically. This is the in-flight twin of §3's "never assume
standard on error", and the reason the boot and in-flight halves differ only in their terminal
state: at boot there is nothing correct to fall back to, so an unreadable mode is `FAULTED`; in
flight the local ledger is still correct, so it is a freeze. The alert carries **why** it froze
(changed vs unreadable) so an operator is not left guessing which.

**The alert is `ACCOUNT_MODE_UNVERIFIED`** (named event `account.mode_unverified`), on the same sink
as the Tier-1 heal-alert and distinct from ADR-0040 §6's `VALUATION_DIVERGENCE` and ADR-0044 §10's
`LEVERAGE_DIVERGENCE`. Those two report a *number* disagreeing with the venue; this one reports that
the numbers can no longer be compared at all — the cross-check has stopped, not diverged. It is
therefore **not** a `*_DIVERGENCE`, and its name says so. ADR-0045 §2's catalog note is updated to
list it beside the other two.

## 5. ADR-0040 §6's alert band: the relative term scales by notional

**Decision: `alert iff |computed − venue| > max(atol, rtol × reference)`, where `reference` is the
notional the quantity's mark-sensitivity flows through. `rtol = 0.001` and `atol = $0.01` are
unchanged. The relative term is never scaled by the compared value.**

This resolves the defect ADR-0040 §6 records and defers here. #142 established that every formula in
this surface reproduces the venue **exactly** when fed the venue's own mark, so the band absorbs
**mark skew only** — one input, from which every quantity's error follows. For a relative skew `ε`:

| quantity | error | reference |
|---|---|---|
| `notional` | `notional · ε` | itself |
| `unrealized_pnl` (position) | `notional · ε` | position `notional` |
| `unrealized_pnl` (account) | `≤ Σ notional · ε` | **total notional** |
| `equity` | `≤ Σ notional · ε` | **total notional** |
| `margin_used` (cross) | `notional · ε / leverage` | itself |
| `margin_used` (isolated) | `notional · ε` | position `notional` |
| `maintenance_margin` | `notional · margin_maint · ε` | itself |
| `free_margin` | `≤ Σ notional · ε · (1 + 1/leverage)` | **total notional** |

**#142's recommendation was half right.** It proposed `notional` for `unrealized_pnl` — correct —
and **`equity` for `free_margin`** — incorrect under leverage. At leverage `L`, equity is roughly
`notional / L`, so scaling by equity makes the band **`L` times too tight**, false-alerting on
exactly the leveraged positions this surface exists to model. The stable reference is the notional,
at both grains.

**Headroom, stated so it is not re-derived later and misread as a regression.** #142 measured a 3 s
p99 skew of `1.4e-04`, giving `rtol = 0.001` about **7×** headroom for the self-scaled quantities.
The tightest case here is `free_margin`, bounded by `(1 + 1/L)·ε·Σnotional ≤ 2.8e-04·Σnotional` at
1x — about **3.6×**. That is comfortable, and `rtol` deliberately stays at `0.001`: the alternative
is widening the band for every quantity to protect the worst one.

`atol` is now what it should always have been — **a pure rounding floor**.

## 6. Liquidation price: `null` is the majority case for a long, and paper must mirror it

**Decision: the venue reports `liquidationPx: null` when the position's liquidation price would be
non-positive. Paper mirrors it exactly: a computed `liq_price ≤ 0` reports `None`.**

#142 observed a held **cross** position report `liquidationPx: null` while the isolated position it
had just closed reported one, and flagged the cause as unverified — *plausibly* unified backing the
cross position with the whole spot balance. **That hypothesis is wrong.** The rule is arithmetic and
mode-independent: a long's liquidation price falls below the mark and can cross zero once collateral
is large relative to notional; a **short's** liquidation price sits above the mark and is always
positive, so it can never be null.

That yields a falsifiable prediction, tested across 29 cross positions on 22 mainnet accounts:

| | long | short |
|---|---|---|
| `liquidationPx: null` | **12** | **0** |
| non-null | 5 | 12 |

**Every null was a long; not one short.** And it is not rare — **12 of 17 cross longs read `null`**,
the majority case. (A quick estimator dropping the `(1 − l·side)` term misclassified 4 of 29, all in
the direction *venue nulled, estimate positive*, consistent with a fuller margin-available term.)

**The read-through itself is fine** — the field is already nullable, ADR-0041 §6 already makes
strategies handle `None`, and ADR-0034's freeze-never-fabricate rule is unchanged. What the
measurement exposes is a **paper/live divergence ADR-0034's core grain forbids**: ADR-0040 §3's
paper branch computes the canonical formula and says nothing about a non-positive result, so on
paper a well-collateralized long would report a **negative liquidation price** while the identical
position on live reports `None`. A strategy tested against a negative number would meet `None` in
production, in the common case.

The threshold is **zero**, not the venue's minimum tick: zero is the real boundary — the price at
which a long's collateral is exhausted — and clamping at a tick would invent a liquidation price for
a position that has none. No `Portfolio` API change: the field is `Decimal | None` already
(ADR-0041).

## Consequences

- **A documented, non-negotiable deployment precondition.** Running Tickwright live against
  Hyperliquid requires the account in Manual/Standard. ~88 % of the sampled accounts that answered
  are not (§1), so most first-run attempts by a new operator will hit the refusal — by design, since
  the alternative is reporting an equity that is off by an order of magnitude.
- **The error and alert catalogs each gain one name.** `VenueAccountModeUnsupported` joins
  `StoreAccountMismatch` and `VenueLeverageMismatch` as an `InvariantViolation` that refuses startup
  (§3); `ACCOUNT_MODE_UNVERIFIED` / `account.mode_unverified` joins `VALUATION_DIVERGENCE` and
  `LEVERAGE_DIVERGENCE` on the reconcile alert sink (§4), and ADR-0045 §2's catalog note lists it.
  Both land with their emitting path under ADR-0020's one-slice-at-a-time rule, not before.
- **The precondition costs no configuration.** Nothing is declared, so nothing can be declared
  wrongly; the venue is asked, and the answer is not overridable. This is deliberately unlike
  ADR-0044's leverage, which *is* config-authoritative — the difference being that leverage is a
  choice and the abstraction mode is a fact about the account's shape.
- **`withdrawable` leaves the model entirely.** CONTEXT.md's `Equity & Free margin` entry lists it
  under `_Avoid_` as "the venue's name for free margin" — that gloss is now wrong and is corrected:
  it is a *different quantity*, not another name for ours. Two further asides that named it as our
  free-margin source are corrected in place for the same reason: **ADR-0040 §3**'s enumeration of
  the live cross-check fields (which also gains the `crossMarginSummary` pair that replaced it) and
  **ADR-0042 §6**'s description of the live account's opening state. `withdrawable` now appears in
  this repo only as a venue field we describe, never as one we read.
- **One venue field became two.** `free_margin`'s cross-check now reads two fields off
  `crossMarginSummary` instead of one root field. No extra request: both arrive in the same
  `clearinghouseState` response the reconcile pull already makes.
- **#142's account-grain measurements are superseded, not merely re-labelled.** Every number it
  took at account grain was measured under a mode this ADR rules out. Its *position*-grain results
  stand — the liquidation formula, the cross formulas, `closedPnl` being gross, the mark-skew
  series — because none of them depend on the mode. The account-grain half is carried by the
  re-validation task graduated from this ticket.
- **The `None` liquidation branch is now understood rather than tolerated.** It was documented as
  though exceptional; it is the majority case for a long, on both paths, for a reason that has
  nothing to do with account modes.
- **Invariant #8** (`docs/agents/invariants.md`) records the precondition and its two verification
  points, because a silent violation corrupts durable state rather than merely reporting wrongly.
