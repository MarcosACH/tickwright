# Supported account abstraction mode: Manual/Standard only, and where account-grain equity and free margin are read

_Accepted via the D13 grilling session on decision ticket [#148](https://github.com/MarcosACH/tickwright/issues/148), graduated from the [#142](https://github.com/MarcosACH/tickwright/issues/142) testnet validation task on the trade-economics map [#107](https://github.com/MarcosACH/tickwright/issues/107). Fixes the premise every account-grain decision in this surface rests on: **that `clearinghouseState` reports the account's equity**. It does — under exactly one family of venue account modes, which this ADR makes a deployment precondition and verifies at boot. **Amends ADR-0034** (the anchor's field sources), **ADR-0040** (§2's `free_margin` source **and its account `maintenance_margin` cross-check scope**, §3's liquidation `null` rule and its paper mirror, §6's alert-band reference — resolving the defect §6 carries a `#148` pointer for), **ADR-0044** (the boot step ordering), **ADR-0024** (startup step 4 opens with the mode gate, and the barrier-failure policy gains a third consumer), and **ADR-0045** (§2's alert catalog gains `ACCOUNT_MODE_UNVERIFIED`, and its frontier bullet records the map's decision frontier closing). **ADR-0042 and ADR-0043 stand decisionally unchanged**, with the precondition their formulas depend on now named — ADR-0042 §6 additionally takes an incidental in-place correction, its aside naming free margin as root `withdrawable` being superseded by §2 here, and **ADR-0041 §4** two narrower ones, correcting where in the response `withdrawable` lives and narrowing its account-Σ maintenance cross-check to the cross subset (§2.1)._

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

**Consequence, stated plainly: this is real friction for anyone else adopting Tickwright.** Three
independent samples of addresses that traded on mainnet the day this was decided, drawn from the
venue's public leaderboard and classified with one unsigned `userAbstraction` read each — **B and C
drawn cleanly; A is not a clean random sample**, for the reason below the table:

| sample | classified | `unifiedAccount` | `portfolioMargin` | `disabled` | `default` | **refused** |
|---|---|---|---|---|---|---|
| A | 114 | 93 | 7 | 9 | 5 | 87.7 % |
| B | 120 | 94 | 10 | 14 | 2 | 86.7 % |
| C | 70 | 61 | 3 | 3 | 3 | 91.4 % |
| **combined** | **304** | 248 | 20 | 26 | 10 | **88.2 %** |

So **roughly 88 % of leaderboard-ranked active traders are on a mode Tickwright refuses.** Sample A
stopped at 114 of its 170 candidates because the run that produced it exited once it had found its
fourth `default`-mode account **holding open positions** — a narrower count than the table's
`default` column, which classifies every address the run drew and therefore reads 5. It is still a
stopping rule that **over**-samples `default` and therefore *under*-states the refused share, which
makes A's 87.7 % a floor rather than a figure biased high. Samples B and C were drawn afterwards
specifically to check it, classify every address they draw, and bracket A from both sides.

**What the ratio is robust to, and what it is not.** Three draws settle the **draw**: sampling noise
and A's stopping rule are both retired. They do not settle the **frame** — all three are drawn from
the public leaderboard, a top-N ranking by PnL and volume, and unified account and portfolio margin
are capital-efficiency features that larger accounts adopt preferentially. So 88 % is a statement
about leaderboard-ranked traders and is plausibly an over-estimate for the whole active population.
That cuts the right way for this decision rather than against it — the friction is real at the top
of the distribution, which is where high-volume automated users sit — but the figure is not a
population proportion and is not used as one. The refusal is designed as a remediation, not a
complaint (§3).

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
- **Source the account-grain numbers from `activeAssetData.availableToTrade`** instead of the
  account summary. Rejected on **grain** and on **meaning**, not on cost — ADR-0044 §4 establishes
  it as an unsigned info read whose price against the address budget is zero. *Meaning — the
  decisive objection:* it returns **two** values rather than one, and what it answers is *"how much
  can I trade on this symbol"* — an admission-gate question, which is the same category error §2
  rejects `withdrawable` for, one grain down, and its exact relationship to our free margin under
  Manual/Standard is **unmeasured** (whether it nets off resting-order margin the way `withdrawable`
  does is precisely the open question). *Grain:* it is **per-symbol** and leaves `equity` unsourced
  entirely — no per-symbol tradability field carries it — so at best it could source one of the two
  account-grain numbers, from one request per symbol, where `crossMarginSummary` sources both from
  the response the reconcile pull already makes.

  Stated carefully, because the obvious grain argument does not hold: under **cross** margin the same
  free collateral backs every symbol, so a single symbol's value already *is* the account figure —
  [#142](https://github.com/MarcosACH/tickwright/issues/142) measured `availableToTrade = 951.66`
  against an equity of `977.58` and `totalMarginUsed` of `25.9168`, which is exactly
  `equity − total_margin_used`. The route is rejected for what the number **means** and for what it
  cannot reach, not because no arithmetic recovers free margin from it.
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
cross-check source. Chasing the residual is carried by the re-validation task graduated from this
ticket ([#152](https://github.com/MarcosACH/tickwright/issues/152)).

**(That last dismissal was wrong, and the rule it dismissed is the answer —
[#152](https://github.com/MarcosACH/tickwright/issues/152).** The `max(initial_margin, 0.1 ×
total_position_value)` transfer rule **does** bind: it binds whenever blended leverage exceeds 10x,
which for a 50-position book is unremarkable. The venue's full withdrawal rule is

```
withdrawable = max(0, accountValue − max(IM, 0.1 × totalNtlPos))
```

so on an account with no resting orders the residual this section calls unattributed is exactly
`max(0, 0.1 × crossNtlPos − crossTMU)`. Re-sampled across **220** mainnet addresses (70
standard-mode, 39 holding no orders of any kind): exact to **one ulp** — the venue reports
`withdrawable` to 6 dp — on **8 of the 10** that hold positions, with the floor actually binding on
6. The clean control, three cross positions all at 20x and zero orders:

| | |
|---|---|
| `crossAV` | `1008998.241837` |
| `crossTMU` | `503510.150066` |
| `crossNtlPos` | `10070203.0013389997` |
| `crossAV − max(crossTMU, 0.1 × crossNtlPos)` | `1977.94170310003` |
| `withdrawable` (actual) | `1977.941704` |

Its whole residual — `503510.150067`, three orders of magnitude larger than the 271.07 — is the
floor. Reproduced on our own testnet account too, at **error `0.000000`**: a zero-order cross short
of notional `5873.49` gave `929.919372 − 587.349 = 342.570372`, the venue's `withdrawable` exactly.

**Two refinements follow.** First, the order-margin term in the table above is specifically
*exposure-increasing* order margin: an order on the opposite side of an existing position reserves
nothing, whether or not it carries `reduceOnly` — control `0xca230e816b…`, 84 HYPE sells against a
HYPE long, none flagged, `withdrawable == crossAV − crossTMU` to 1e-10. Second, `0x6de6…ef7b` itself
can no longer be re-checked (only its abbreviated address survives, and its state has long moved), so
the identification is of the **class**, not of that one snapshot.

**The decision does not change — it gets a second, independent reason.** The identified term is a
*withdrawal haircut*: a 10 % floor on notional that exists to stop an account being drained to the
edge of its maintenance margin. It is not free collateral under any reading, so `withdrawable`
answers a third question this surface never asks, on top of the resting-order margin above. What
this correction removes is the suggestion that the field holds something mysterious; what remains
unidentified is much **narrower** — no longer a term every account may carry, but 2 of the 10
sampled accounts, both heavily-positioned books, at magnitudes *larger* than the 271.07:

| account | positions | model predicts | venue `withdrawable` | unexplained |
|---|---|---|---|---|
| `0xd4c1f7e8d8…` | 17 | `379767.931283` | `311553.645131` | **`68214.286152`** |
| `0x27c9fa86c9…` | 61 | `73502.120231` | **`0.0`** | **`73502.120231`** |

Both are ruled out by direct computation as orders, margin tiers, isolated positions, or spot. But
they are **two different shapes, not one class**, and only the first is a further *deduction*: the
second has `withdrawable` **pinned at exactly zero** while the model predicts a positive figure, so
what fails there is not an unaccounted term of the same kind but the field bottoming out under some
condition this rule does not express. Whoever revisits this should treat them separately.

(Figures are as at the sampling snapshot. Both accounts trade actively, and a tight re-read minutes
later gave `68281.421247` and `64035.283183` respectively — the magnitude class is stable, the exact
number is not, which is why the snapshot is named.)**)**

### 2.1 The same scope error, one field over: account `maintenance_margin`

**Decision: account-level `maintenance_margin` is reported as Σ over *all* positions, but
cross-checked against `crossMaintenanceMarginUsed` over the **cross subset only**.**

ADR-0040 §6 puts account-level `maintenance_margin` in the alert band, and R1
([#108](https://github.com/MarcosACH/tickwright/issues/108)) recorded its venue counterpart as root
`crossMaintenanceMarginUsed` without recording that field's **scope**. It is cross-only — the name
says so — while ADR-0040 §2's account row is a Σ over every position. Measured:

| account | venue `crossMaintenanceMarginUsed` | our Σ over **cross** | our Σ over **all** |
|---|---|---|---|
| `0x48cd…4955` — 2 cross, 0 isolated | 2141.6235 | 2141.6235 ✓ | 2141.6235 ✓ |
| `0x6de6…ef7b` — 50 cross, 0 isolated | 4351.774296 | 4351.774308 ✓ | 4351.774308 ✓ |
| `0x863a…fd6f` — 0 cross, 1 isolated | **0.0** | 0.0 ✓ | **11028.12** ✗ |

The trap is a **venue asymmetry between two fields of the same response**: `marginSummary.totalMarginUsed`
**includes** isolated positions (measured `111169.464955` on that third account, exactly its isolated
position's `marginUsed`), while `crossMaintenanceMarginUsed` **excludes** them. So `margin_used`'s
account Σ cross-checks correctly and `maintenance_margin`'s does not, and nothing in the field names
warns you which is which except the `cross` prefix on the second.

This is §2's defect one field over, with the same signature: an unbounded, mode-independent gap that
no `rtol`/`atol` absorbs, firing whenever an isolated position is open — which ADR-0040 §1 calls
**the primary mode in practice**. It is *not* the mark-skew the band was sized for.

**Re-scope rather than drop**, mirroring §2: the reported number stays Σ-over-all, because that is
the honest account-wide maintenance a strategy should read, and only the **comparison** narrows to
the cross subset. Dropping account `maintenance_margin` from the band would also discard the signal
on the cross subset, where the venue does publish a number. The cost is that **isolated maintenance
has no venue cross-check at all** — the venue publishes neither a per-position maintenance field
(R3 #110) nor a total — so it is computed-only, and ADR-0040 §4's tier-crossing alert reaches only
cross positions. Stated rather than hidden.

**`free_margin` is the deliberate exception, and it must not be narrowed the same way.** Its venue
side is *also* a cross-scoped pair (§2: `crossMarginSummary.accountValue −
crossMarginSummary.totalMarginUsed`), so the rule this section establishes — venue field is
cross-scoped ⇒ narrow the comparison — reads as if it applied. It does not. Our computed
`free_margin` is already the cross pool: §2 derives the cancellation exactly, the isolated collateral
carried inside `cash` against the isolated `margin_used = collateral + unrealized_pnl`. Narrowing
*our* side as well would subtract isolated collateral a second time. The two sections differ because
the venue's own arithmetic differs: `crossAV` and `crossTMU` each drop the same isolated term and it
cancels in their difference, whereas `crossMaintenanceMarginUsed` drops an isolated term with nothing
to cancel against. So: `equity` compares Σ-over-all against an all-scope field; `free_margin`
compares Σ-over-all against a **cross-scoped pair**, exact only by that cancellation and therefore
**not** narrowed; `maintenance_margin` alone narrows to the cross subset.

**A confirmation falls out of the same measurement.** `0x6de6…ef7b` reproduces
`crossMaintenanceMarginUsed` across **50 distinct symbols** to ~3e-9 relative, which confirms
ADR-0040 §4's flat `margin_maint = 1/(2·max_leverage)` far more broadly than
[#142](https://github.com/MarcosACH/tickwright/issues/142)'s single BTC position could — and confirms
it below each symbol's first tier band, which is where §4 says the flat rate is exact.

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

**These four literals are not §1's four modes**, and the coincidence of counts is worth naming:
Manual/Standard supplies **two** of them, and DEX Abstraction — legacy and being discontinued —
supplied none in anything sampled. The allowlist is written against the literals, so whatever that
mode reports, it refuses.

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
- **The error is a remediation.** Given that ~88 % of the sampled leaderboard accounts are on a
  refused mode (§1), it names the observed mode, the two accepted literals, and the fix:
  `userSetAbstraction("disabled")`
  with the master wallet, then a spot→perps `usdClassTransfer`. Both are **user-signed** actions an
  agent wallet cannot perform, which is why this is an operator step and not something the engine
  can do for itself.

  **(Both halves of that claim were executed and confirmed —
  [#152](https://github.com/MarcosACH/tickwright/issues/152).** The remediation was run end-to-end on
  the testnet account and produced the literal **`"disabled"`**, which is the whole reason this
  allowlist has two entries: a `== "default"` check would have refused the account the operator was
  just told to build.

  The agent-wallet half deserves an explicit note, because the pinned SDK **appears to offer a way
  around it**. Alongside the EIP-712 `user_set_abstraction`, `hyperliquid-python-sdk` 0.24.0 exposes
  **`agent_set_abstraction(abstraction)`**, signed via `sign_l1_action` — the *agent-signable* path,
  the same one orders use. It is not a loophole. Tested against the venue:

  | call | signing path | venue response |
  |---|---|---|
  | `agent_set_abstraction("i")` | `sign_l1_action` (agent-signable) | `err: Abstraction transition not allowed` |
  | `user_set_abstraction(master, "disabled")` | EIP-712 user-signed | `err: Must deposit before performing actions. User: 0x7bc6…d273` |

  The second is the general rule and worth stating once: **a user-signed action is attributed to the
  signer, not to the `user` field in its payload**. An agent key is therefore treated as its own
  (empty) account and can never act for the master, whatever address the payload names.**)**
- **Paper is a no-op.** The mode is a live-only concept, and no live run may read a paper block or
  vice versa (ADR-0042 §1).
- **No configuration surface.** The mode is read from the venue, never declared. `.env.example` gains
  nothing.

**(Shipped in [#179](https://github.com/MarcosACH/tickwright/issues/179)** as
`venues/hyperliquid/preflight.py`, called from `HyperliquidExchange.start()`. Two implementation
facts this section did not fix, both settled by the code:

*The barrier budget reaches the adapter through the composition root — and is a number, not a
window.* The gate runs at step 4 and so cannot **be** a barrier step — the barrier is step 5, and
`venues` may not import `engine` (ADR-0032) — but bounding it on a timeout of its own would be the
second timeout ADR-0044 §6 refuses. `build_exchange` therefore hands `HyperliquidExchange` the
engine's `startup_reconciliation_timeout_seconds`, pinned by a composition-root test that builds the
arm on a non-default budget and spends it against a dark venue: at the 60 s default a hard-coded
timeout would be indistinguishable from a wired one. The 1 s/30 s doubling is the venue package's
own `Backoff`, shared with the feed's reconnect loop, so only the deadline arithmetic is written
twice — here and in `StartupBarrier.run`, on opposite sides of the layer rule. Nothing new is
configurable.

What is shared, though, is that **number** rather than one wall-clock window. `start()` is bounded
by the budget and the barrier one step later opens a fresh one, so a venue that clears the gate late
and then goes dark reaches `FAULTED` after up to two budgets plus each loop's capped overshoot —
which is worth stating precisely because both loops argue at length against a ~2× overshoot. A
single boot deadline would have to be passed *into* `Exchange.start()`, a change to that signature
and nothing an adapter can arrange for itself; ADR-0044 §7's second guard, which will otherwise open
a third window of its own, is the point at which it becomes worth making. The `Exchange.start()`
contract in `domain/protocols.py` carries this, so "one boot-time budget, never a second timeout" in
the adapters reads as the configured number and never as a bound on boot time.

*A body that is not a mode literal is a failed read, not an unrecognised one.* This section splits
"unreadable" from "unrecognised" but states the split against the **mode**, and the venue answers
this query with a bare JSON string — so there is a third case it does not name: a response that is
not a string at all. That is the venue changing its contract, it says nothing about the mode, and it
takes the **retry** rather than the allowlist's refusal. The distinction is load-bearing in the
error text as much as in the control flow: printing the remediation there would send an operator to
re-set a mode that was never in question.**)**

**(Amended by [#180](https://github.com/MarcosACH/tickwright/issues/180)** — the block above names
the point at which a single boot deadline becomes worth making, and ADR-0044 §7's push reached it.
Two of its statements are now retired.

*The boot spends two windows, not three, and the signature did not change.* The block reasons that a
shared deadline "would have to be passed *into* `Exchange.start()`". It does not: the two guards are
both **inside** `start()`, so the adapter opens one `Deadline` off the budget it was handed and
spends it across the mode gate and the push. Whatever the gate spends retrying is gone from what the
push has left. A boot therefore still spends at most two windows — `start()`'s and the barrier's,
exactly as this block describes — and adding a third guard behind these cannot quietly make it
three. `Exchange.start()`'s contract in `domain/protocols.py` is unchanged and still correct: what
crosses that seam is the configured **number**, and one wall-clock window per side of it.

*The pacing is no longer the venue package's own.* "The 1 s/30 s doubling is the venue package's own
`Backoff` … so only the deadline arithmetic is written twice — here and in `StartupBarrier.run`, on
opposite sides of the layer rule" was true and is not. Both are `domain` values now
(`domain/pacing.py`: `Backoff`, `Deadline`), which is the one package `engine` and every adapter may
both import, so the layer rule never required the duplication — only a shared home below both sides
of it. The *loops* still differ and deliberately so: a barrier step reports a freeze as a `False`
return while these guards catch a venue-specific transient tuple, and hoisting a loop over both
would carry the venue's read vocabulary into `domain` against ADR-0031.**)**

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

**(Shipped in [#195](https://github.com/MarcosACH/tickwright/issues/195), with the freeze narrower
than "freeze the account-grain reconcile" reads.** This section's decision holds unchanged; two
things it left to a slice are now fixed, and one word in it is easy to over-read.

**The verdict crosses the `Exchange` seam; the literals do not.** The re-read reaches the engine as
a new seam member, `async verify_account_mode() -> AccountModeVerdict`, rather than the cycle
calling the boot gate this ADR shares its allowlist with: the caller is engine-internal and `engine`
may not import `venues` (ADR-0032), while `userAbstraction`'s accepted literals are venue knowledge
that may not move to `domain` (ADR-0031). What that buys is exactly what "reuses the boot gate's
read" was after — one module owning the mode on both paths — with the venue answering and the engine
deciding. The verdict is three-valued (`VERIFIED` / `CHANGED` / `UNREADABLE`) where the control flow
needs two, because this section's own requirement is that the alert says *which*: the last two are
one branch and two records. Live answers off one unsigned read with **no retry** — the boot budget
is a startup concept and the cadence is the in-flight retry — and paper answers `VERIFIED`
permanently, having no abstraction mode to be in.

**"Freeze the account-grain reconcile" means the heal, not the classification.** What is refused is
the cash correction; the pass still reads the anchor, still classifies both tiers, still returns its
findings and still records `account.reconciled`. That is deliberate and it is not the freeze
`fetch_account_state() -> None` performs, which returns nothing because there was no snapshot to
compare against: here the snapshot is real and only its *account-grain meaning* is in doubt, so
discarding the position-grain findings computed from it would throw away good work — and the size
heal, which a pooled mode does not touch, keeps booking on the same transaction the cash correction
was dropped from. The consequence for whoever lands the alert bands
([#194](https://github.com/MarcosACH/tickwright/issues/194)) is that account-grain findings **are
still counted while frozen**, so suppressing the alerts that would otherwise fire off a snapshot
this section has just declared unreliable is that slice's job, not this one's.

**One cost of the placement, accepted and not free.** The re-read is the cycle's
one **yield point between classifying and writing**: the cadence runs beside the saga in the
runner's `TaskGroup`, so a fill checkpointed while the mode read is in flight has its cash effect
overwritten by a correction computed before it — the cash line is healed by assignment to a target,
not by a delta. That is ADR-0034's own one-cadence over-read reached from a new direction, and it
converges the same way: the next snapshot carries the fill, the exact cash comparison finds the gap,
and the pass after heals it. Re-reading the ledger side after the verdict would not close it — the
venue snapshot predates the await too, so the recomputed target is the same figure against a book
the venue has not seen — and the sound response to "a fill landed" is the pass this already is, one
the next deadline repeats.**)**

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
| `maintenance_margin` (account, **cross subset**) | `≤ Σ_cross notional · margin_maint · ε` | itself — **the cross-subset Σ**, never the reported Σ-over-all (§2.1) |
| `free_margin` | `≤ Σ notional · ε · (1 + 1/leverage)` | **total notional** |

Two readings the table has to rule out. **"Itself" means our computed quantity**, not the venue's:
the defect §6 of ADR-0040 records is scaling by `|venue|`, and where the reference is "itself" the
quantity is proportional to the notional its error flows through (`notional`; cross `margin_used =
notional/L`; `maintenance_margin = notional · margin_maint`), so self-scaling *is* notional-scaling.
And **`maintenance_margin` is the one row where the reported and compared quantities differ** — §2.1
narrows the comparison to the cross subset while the reported number stays Σ-over-all, so the
reference is the cross subset's Σ. Using the reported Σ-over-all would widen the band by
`Σ_all / Σ_cross`, which is unbounded: on an account holding `Σ_cross = 100` against `Σ_iso =
10 000`, `rtol = 0.001` gives a band of `10.10` where the cross-subset reference gives `0.10` — a
10 % divergence on the cross subset would never fire, suppressing exactly the tier-crossing signal
ADR-0040 §4 says the alert exists to raise.

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
  Hyperliquid requires the account in Manual/Standard. ~88 % of the sampled leaderboard accounts
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
  **ADR-0042 §6**'s description of the live account's opening state. **ADR-0041 §4**'s
  venue-faithful placement note takes a third, narrower correction: it listed the field as a
  `marginSummary` member, where it is a field of the response **root**. `withdrawable` now appears
  in this repo only as a venue field we describe, never as one we read.
- **One venue field became two.** `free_margin`'s cross-check now reads two fields off
  `crossMarginSummary` instead of one root field. No extra request: both arrive in the same
  `clearinghouseState` response the reconcile pull already makes.
- **A venue field's *scope* is now part of what sourcing it means.** Both defects this ADR fixes
  are the same mistake — reading a field whose name suggests our quantity and whose scope is
  narrower (`withdrawable` nets off **whichever is larger** of total initial margin — positions
  *and* exposure-increasing resting orders — or a 10 %-of-notional floor, usually the floor, §2;
  `crossMaintenanceMarginUsed` excludes isolated positions, §2.1) — and one response carries fields
  on **both** scopes (`marginSummary.totalMarginUsed` includes isolated,
  `crossMaintenanceMarginUsed` does not). Every account-grain member of ADR-0040
  §6's band now carries its comparison scope: `equity` and `margin_used` compare Σ-over-all against
  all-scope venue fields; `free_margin` compares Σ-over-all against a **cross-scoped pair**, which is
  exact only by the cancellation §2 derives and is therefore **not** narrowed; `maintenance_margin`
  alone narrows to the cross subset. **Isolated maintenance margin has no venue counterpart at all**
  and is therefore computed-only.
- **#142's account-grain measurements are superseded, not merely re-labelled.** Every number it
  took at account grain was measured under a mode this ADR rules out. Its *position*-grain results
  stand — the liquidation formula, the cross formulas, `closedPnl` being gross, the mark-skew
  series — because none of them depend on the mode. The account-grain half is carried by the
  re-validation task graduated from this ticket,
  [#152](https://github.com/MarcosACH/tickwright/issues/152).
- **The `None` liquidation branch is now understood rather than tolerated.** It was documented as
  though exceptional; it is the majority case for a long, on both paths, for a reason that has
  nothing to do with account modes.
- **Invariant #8** (`docs/agents/invariants.md`) records the precondition and its two verification
  points, because a silent violation corrupts durable state rather than merely reporting wrongly.
