# Applying leverage & margin mode to the venue: one boot-time push, a refusal on held disagreement, and a config that never fights the operator

_Accepted via the D11 grilling session on decision ticket [#141](https://github.com/MarcosACH/tickwright/issues/141), part of the trade-economics map [#107](https://github.com/MarcosACH/tickwright/issues/107). Discharges the venue-write question **ADR-0040 §5** deferred ("the engine does not set leverage or mode on the venue as part of this surface … captured as a separate decision ticket") — and **amends ADR-0040 §5 twice**: the config block moves out of `PaperExchangeConfig`, and its claim that a leverage disagreement surfaces through `margin_used` is corrected (it is blind for isolated positions). **Extends ADR-0024** (startup step 4 gains the connect half its prose already promises) and **declares on the `Exchange` Protocol the `start()` ADR-0014 already assigns it**. Grounded in the `updateLeverage` surface captured verbatim by R3 ([#110](https://github.com/MarcosACH/tickwright/issues/110))._

ADR-0040 made the per-symbol leverage and margin mode the single source of truth for the margin
model on both paths, then stopped at the boundary: nothing pushed that truth to the venue, so a
live run computed margin, liquidation price and effective leverage from config while the venue
traded on whatever the operator had last set in its UI. That gap is not a reporting nicety — venue
leverage decides the collateral actually locked and the price at which a real position is actually
liquidated. This ADR fixes when the engine writes leverage and mode to the venue, what it does when
the two disagree, and which of the venue's account-management actions it declines to own.

## 1. One push, at boot, and never again

**`HyperliquidExchange` aligns the venue to config exactly once, during startup.** After that the
venue is left alone: a divergence is *alerted* (§10), never re-pushed.

Config wins at boot; the venue wins in-flight. The alternatives both fail on a specific case:

- **Never pushing** and letting the venue win everywhere would make config-authority (ADR-0040 §5)
  a fiction on live and leave the operator maintaining the same numbers in two places, with the
  model silently reporting whichever it was told.
- **Re-pushing on drift** each reconcile — continuous convergence — is the option that looks most
  principled and is the most dangerous. An operator who lowers leverage in the venue UI to de-risk
  a live position would have that change silently reverted within one reconcile cycle. The engine
  must not out-argue a human at the venue. It also spends the wrong budget: the venue's
  address-based rate limit starts at **10 000 requests and accrues at 1 per USDC traded**, so an
  unconditional per-cadence write would consume a low-volume account's lifetime allowance on
  bookkeeping. Verbatim from the venue's rate-limits page, §"Address-based limits" —
  https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits —
  *"Each address starts with an initial buffer of 10000 requests"*, *"The rate limiting logic allows
  1 request per 1 USDC traded cumulatively since address inception"*, and — the half that decides
  what counts against it — *"Note that this rate limit only applies to actions, not info requests."*
  Cited directly rather than through R3's note
  ([#110](https://github.com/MarcosACH/tickwright/issues/110)), which captured the
  margin/liquidation/mark surface and no rate-limit facts at all. The argument does not turn on the
  numbers anyway: the operator-authority reason stands alone, and a *cheaper* write budget would not
  make silently reverting a human's de-risking edit acceptable.
- **Pushing lazily before the first order** on a symbol would move a signed on-chain write onto the
  order path and leave reported margin wrong for any configured-but-not-yet-traded symbol.

Boot is the one moment the engine legitimately owns the account's configuration: nothing of ours is
in flight, the barrier (ADR-0024 step 5) has not yet let an order out, and the operator has just
started the process with the config in front of them.

## 2. The config moves out of the paper adapter: `AppConfig.leverage`

ADR-0040 §5 placed the per-symbol block in `PaperExchangeConfig`. That cannot stand, for a reason
independent of this ADR's push: the component that *consumes* it — `PortfolioProjection`
(ADR-0035, `engine`) — is venue-agnostic and needs it on **both** paths, and no live run may read
`config.paper`. ADR-0042 §1 already litigated that precise hazard for genesis collateral: a
`TICKWRIGHT_PAPER__*` variable must never govern a live run.

So the block becomes a **venue-agnostic top-level `AppConfig.leverage: dict[str, LeverageSpec]`** —
a peer of `strategies` and `engine`, not of the per-adapter configs. `LeverageSpec` is a frozen
`domain` value carrying the pair the venue action itself carries:

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class LeverageSpec:
    mode: Literal["cross", "isolated"] = "isolated"
    leverage: int = 1
```

Mode and leverage are **one value, not two maps**, because `updateLeverage {asset, isCross,
leverage}` sets both in a single signed action: splitting them in config would invent a state
(mode set, leverage unset) the venue has no way to express. Defaults stay ADR-0040 §5's safest
pair — `1x` isolated — so an absent entry is a complete, conservative specification rather than a
hole.

**Naming.** `leverage` / `LeverageSpec`, not `margin` / `MarginSpec`, because `CONTEXT.md` already
binds **Margin** to the *reported collateral a position ties up* — an output of this surface,
**computed** on a cross position and **ingested** on an isolated one (ADR-0045 §9.5; this once read
"a computed Tier-2 output", which is the cross-only half). Naming an operator input with the word
the glossary gives to a reported output would be exactly the collision the glossary exists to
prevent. The glossary's term for what this block holds is **Leverage & Margin mode**, and the field
takes its head noun.

**The composition root resolves the map before injecting it.** `AppConfig.leverage` is *sparse* —
it carries only the symbols the operator wrote — while §3's scope is every symbol the configured
strategies declare. So the root reads it once and resolves the two into a **complete**
`dict[str, LeverageSpec]` covering exactly the strategy-traded set, filling the `1x`/`isolated`
default for each symbol without an entry, then injects **that resolved map** into both consumers:
the `PortfolioProjection` (the model, both paths) and the `Exchange` (the push and the bounds check,
§7/§9).

Resolving here rather than in either consumer is load-bearing twice over. It keeps the model and the
venue reading the same numbers by construction — the two consumers cannot disagree about what an
unconfigured symbol means, which is the disagreement §3 exists to rule out. And it is the only
place both inputs are in scope: `strategies` and `leverage` are peer `AppConfig` fields, while an
`Exchange` knows nothing of strategies. The two symbol sets an adapter *does* hold are both the
wrong ones, and §3 rejects each for its own reason: `HyperliquidConfig.symbols` is the feed
subscription list, which can legitimately carry context symbols no strategy places against, and
`PaperExchangeConfig.instrument_specs` is paper's instrument *universe* — the `meta.universe`
analogue, config-sourced because the paper venue has no meta endpoint (ADR-0031) — which §9 reads
for `max_leverage` and which says nothing about what is traded. The adapter therefore iterates a
map it needs no strategy knowledge to interpret, which is also why this sync does not thicken it
(ADR-0015).

## 3. Scope: every strategy-traded symbol, including the ones nobody configured

The push covers **every symbol the configured strategies declare** — its `AppConfig.leverage` entry
where present, the `1x`/`isolated` default where absent.

Two scopes were rejected on sight. The venue's `meta.universe` (which `HyperliquidUniverse` holds
in full) is ~200 perps — 200 signed writes per boot for an engine that trades three of them. And
the feed's subscription list can legitimately include symbols carried for context that no strategy
places against. What the process can place orders on is what the strategies declare, and that is
the set whose leverage must be true.

The live question is the **strategy-traded symbol with no config entry**, and it is settled by which
way the two failures point:

- **Not pushing it** leaves the model computing at `1x`/isolated while the venue may sit at 20x
  cross. The report then shows full-notional margin and a distant liquidation price for a position
  that is in fact 20x leveraged — it **understates** real risk, the dangerous direction, caught only
  after the fact by a divergence alert.
- **Pushing the default to it** resets the venue to the pair ADR-0040 §5 chose *because* it is the
  safest, and the model matches reality. The surprise is a de-risking one.

Pushing the default also gives the divergence signal a clean meaning: after boot, any disagreement
is real drift, never a config gap.

**An entry for a symbol no configured strategy trades is rejected at startup**, with the offending
keys named — as an `AppConfig` cross-field validator over `strategies` × `leverage`, the shape
`config.py` already uses to validate `hyperliquid.symbols` against the selected feed, and
ADR-0042 §1 to demand genesis collateral when the exchange is paper. It fires at config load,
before any component is built, so §2's resolution never meets a dead entry and neither path reaches
`start()` carrying one. It is config that cannot take effect — nearly always a typo'd symbol — and
`AppConfig`'s existing `extra="forbid"` already establishes that silently-ignored configuration is
treated here as a bug, not a convenience.

## 4. One read splits the push three ways

Startup performs **one unsigned `clearinghouseState` read**, then treats each strategy-traded symbol
by what that read says:

| Venue state | Action |
|---|---|
| Open position, `leverage.{type, value}` equals config | **skip** — no write at all |
| **No** open position | **write** unconditionally |
| Open position, disagrees with config | **refuse to start** (§5) |

The split follows the only line that matters: **the risky symbols are exactly the ones holding a
position**, and `clearinghouseState.assetPositions` reports leverage for exactly those. A symbol
with no position has nothing to re-margin and nothing to reject on position grounds, so a blind
write is safe and needs no prior knowledge of its stored setting.

This deliberately avoids `activeAssetData` (`{type, user, coin}` → `leverage.{type, value, rawUsd}`),
the one endpoint that reports the setting for a position-less symbol. It would permit a fully
idempotent push, but it costs one info read per symbol instead of one per boot, and **its behaviour
for a symbol with no open position is not documented** — the design would rest on an unverified
premise to save writes that are already free of the address budget's meaningful cost — one boot's
worth of writes against §1's cited 10 000-request buffer. It stays a named option, reachable if the
blind write ever proves noisy.

**([#142](https://github.com/MarcosACH/tickwright/issues/142) verified the premise; the extension
point is now unblocked, and the decision to defer it is a cost trade rather than an unknown.**
Queried against a **flat** BTC on testnet, `activeAssetData` returned
`leverage: {type: "cross", value: 20}` — plus `rawUsd` when the symbol is isolated —
alongside `maxTradeSzs`, `availableToTrade` and `markPx`. It **does** report the setting with no
open position.

Two facts sharpen the trade this section declined. In its favour: `activeAssetData` is an **unsigned
info request**, so by §1's own *actions-not-info* citation it costs nothing against the address
budget — the "one read per symbol" price is smaller than assumed. Against it: the pinned SDK's
`Info` exposes **no `active_asset_data` method**, so it needs a raw `Info.post("/info", …)`, where
`Exchange.update_leverage(leverage, name, is_cross)` exists as a typed call.

Nothing here reverses the decision — the blind write remains correct and simpler. But the idempotent
push is now a real option, and it is the natural home for the `EXCHANGE_LEVERAGE_UNCHANGED` event
§6's correction leaves without a source.**)** (Note the trade is not reads-for-writes at par: by §1's citation the
reads it would add cost nothing against that budget, so what the option actually buys is
idempotency, and what it costs is resting the design on an undocumented premise.)

**The blind write is therefore only as fresh as that one read, and the gap is accepted rather than
closed.** §5's "never write for a held symbol" holds against the venue state the read returned, not
the state at the instant of the write. Two actors can open a position inside it, and the ordinary
one is **ours**. The barrier has not cleared and the feed starts last (ADR-0024), so this process
cannot *place* an order in the window — but that is not the same as no order *filling*: ADR-0024's
graceful stop **deliberately leaves resting `LIVE` orders on the venue**, to be re-adopted by
`cloid` on restart, so a limit order from the previous run sits on the book throughout step 4 and
can fill at any moment. That is not an anomaly; it is the supported, expected state after a clean
restart, and it is the likeliest way a symbol the read saw flat is held by the time we write to it.
The second actor is **foreign flow** — a manual venue-UI trade or a second process on the same
account (ADR-0038) — rarer, and already an ADR-0038 alerting condition rather than a supported mode.

The gap is accepted on two grounds that hold for **either** actor: the window is one startup step
wide, and **no amount of re-reading closes it** — `updateLeverage` offers no compare-and-set, so any
read-then-write stays racy, and a per-symbol re-check would buy a merely smaller window at exactly
the per-symbol cost the paragraph above just declined to pay. The residual risk is narrow and
stated: a position opened during those seconds — most likely by our own resting order filling — is
re-margined at boot instead of refused, in the one case §5 exists for (config and venue disagreeing
on that symbol; where they agree the write is a no-op either way). If
[#142](https://github.com/MarcosACH/tickwright/issues/142) finds the venue *rejects* a change on a
held position, the race closes on its own — the write fails and §6's taxonomy faults the boot,
which is the right outcome anyway.

**Whether a no-op `updateLeverage` succeeds or errors is undocumented** across the venue's own docs
and every SDK surface examined. The skip above removes the question for held symbols, but a
position-less symbol may well already carry the configured setting, so the failure taxonomy (§6)
must treat a no-change error as success. [#142](https://github.com/MarcosACH/tickwright/issues/142)
confirms the real response shape so that tolerance can be narrowed from "looks like no change" to
an exact match.

## 5. A held disagreement refuses to start — the venue twin of `StoreAccountMismatch`

**The engine never writes leverage or mode for a symbol that currently holds an open position.** If
config and venue disagree there, startup raises **`VenueLeverageMismatch`** — an
`InvariantViolation`, ADR-0014's second error class (fail-fast → `FAULTED` → the process exits),
named beside `StoreAccountMismatch` rather than folded into it because the two answer different
questions and the operator resolves them in different places — naming **every** disagreeing symbol
with both pairs, and the process faults before any order can be placed.

This is the same shape ADR-0042 §3 / ADR-0043 §8 already established for the durable store: when
declared config contradicts authoritative external state, **refuse rather than guess which side
wins**, and report every disagreement at once rather than one per restart. The store check answers
"is this my ledger?"; this one answers "is this the account I am modelling?".

Pushing instead was rejected on the venue's own words. Its documentation states that *"the leverage
of an existing position can be increased without closing the position. Leverage is only checked upon
opening a position."* (Verbatim from the [margining
page](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/margining), §"Initial Margin and
Leverage", cited directly rather than through R3's note
([#110](https://github.com/MarcosACH/tickwright/issues/110)) — that note captured the
`updateLeverage` *action shape*, not the leverage-change semantics this section turns on.) If
leverage is checked only at open, then changing it afterwards plausibly governs only future opens,
leaving the live position's actual locked margin as it was — so the push would not make the model
true anyway. And if the venue *does* recompute an open position's margin, the push has silently
re-margined a live position at boot. Both readings argue against writing; the refusal is correct
under either, which is why this ADR does not wait on
[#142](https://github.com/MarcosACH/tickwright/issues/142) to resolve which holds. The venue's
behaviour for a **leverage decrease** or a **mode switch** on a held position is likewise
undocumented — and irrelevant here, because we never attempt one.

**([#142](https://github.com/MarcosACH/tickwright/issues/142) resolved which reading holds: the
first, and the refusal is vindicated on the stronger of the two grounds.** Measured on a held
isolated position, `updateLeverage` at 5x → 10x → 3x left `marginUsed` at `45.858067`, `rawUsd` at
`−83.731933` and `liquidationPx` at `42395.915443038` — **all three unchanged**. A leverage change
after open does **not** re-margin the live position; it governs future opens only, exactly as
*"leverage is only checked upon opening a position"* reads on its face. So a boot push would not
have made the model true, which is this paragraph's first reading.

The two behaviours called "undocumented and irrelevant here" were measured too, and they narrow
§4's accepted read→write race further than this ADR assumed:

- **mode switch on a held position: always rejected** — `"Cannot switch leverage type with open position."`
- **leverage decrease on a held position: conditionally allowed** — 10x → 3x succeeded; 3x → 1x was rejected for insufficient isolated margin
- **leverage increase on a held position: allowed**, as the venue documents

Combined with the no-re-margin result, **every branch of the race is safe**: a write that lands on a
position opened inside the window either succeeds without touching the position's margin, or fails
with a classifiable `err` that §6 faults on. The blind write **cannot silently re-margin a live
position** — the outcome §4 accepts as a residual risk is, on this venue, unreachable. The
uncertainty is removed rather than tolerated.**)**

The gate is quiet in normal operation: after a clean restart config and venue agree by construction
(this engine pushed them into agreement at the previous boot), so it fires only when config changed
while a position was open, or someone hand-edited the venue. Both are moments where stopping is the
service. The operator resolves it by closing the position or by matching config to the venue —
never by the engine picking one.

## 6. Failure policy: ADR-0024's barrier budget, reused

A failed push is **retried with backoff under the existing `startup_reconciliation_timeout`
budget, then `FAULTED` → non-zero exit → supervisor restart.**

The adapter's order-path failure vocabulary (`EXCHANGE_REQUEST_FAILED` for an `OSError`,
`EXCHANGE_ACTION_REJECTED` for a 200-OK `err` envelope) does not transfer: those name a fact and
step back because **reconciliation heals** what was ambiguous. Nothing heals a leverage push. But
it needs no policy of its own either — it runs in the same boot window as the startup barrier and
faces the same transient-blip reality, and ADR-0043 §6 already set the precedent of extending that
budget to a new boot-time venue read rather than minting a second timeout. A boot-time blip
resolves and we proceed; a sustained failure exits into the supervisor's backoff instead of
crash-looping.

Within that: a **no-change `err`** counts as success and emits
**`EXCHANGE_LEVERAGE_UNCHANGED`** (`exchange.leverage_unchanged`, joining ADR-0020's named-event
catalog beside the adapter's two existing write-path events), not a failure (§4); a
**rate-limit** rejection is transient and retried; anything else consumes the budget and faults.
Clearing startup with a venue we failed to align is not an available outcome — that is the state
the step exists to prevent.

**(Corrected by [#142](https://github.com/MarcosACH/tickwright/issues/142) — the tolerance is not
narrowed, it is removed.** This paragraph was written against an undocumented response shape and
hedged accordingly. Measured against the venue: a **no-op `updateLeverage` returns the identical
success envelope as a real change** — `{"status": "ok", "response": {"type": "default"}}`, verified
by pushing `cross/20x` onto a symbol already at `cross/20x`, and again for `isolated/1x`. **There is
no "no change" `err` at all.**

So the taxonomy is **exact**, with no fuzzy match to tolerate: `status == "ok"` ⇒ success;
`status == "err"` ⇒ classify and fault. And **`EXCHANGE_LEVERAGE_UNCHANGED` is unreachable from the
write** — the write cannot distinguish a no-op from a change. It must either be dropped, or
re-sourced from the pre-read now that §4's `activeAssetData` route is confirmed viable (see the §4
correction), where "already aligned" is knowable *before* writing. That choice belongs to whoever
implements §4's push.

The error envelope is `{"status": "err", "response": "<plain string>"}` — a **bare string**, and
returned as a **value, not raised**, so the adapter must inspect the envelope rather than rely on
exceptions. Three concrete strings observed, for this section's classification:

| observed `response` | cause | classify as |
|---|---|---|
| `"Invalid leverage value"` | leverage above `maxLeverage` | fault — a config bug §9 should already have caught |
| `"Isolated position does not have sufficient margin available to decrease leverage. To decrease leverage, add margin to the position."` | decrease on a held position with too little collateral | fault (§5's held-disagreement outcome) |
| `"Cannot switch leverage type with open position."` | mode switch on a held position | fault (§5's held-disagreement outcome) |

Note the venue enforces the leverage bound itself, so §9's both-paths validation is
belt-and-braces — correctly, since it fires at config load rather than at boot.**)**

## 7. The seam: `Exchange.start()`, at ADR-0024 step 4

The `Exchange` Protocol gains **`async def start(self) -> None`**, called by
`Engine._start_sequence` at step 4. `PaperExchange.start()` performs the §9 validation and no write;
`HyperliquidExchange.start()` validates, reads, and pushes.

This declares a method the architecture already assumed rather than inventing a seam. ADR-0014
names `Exchange` among the long-lived components with `async start()` / `async stop()`; the Protocol
simply never declared them. ADR-0024 step 4 already reads *"Connect the `Exchange` +
`ExecutionManager`"* while only the subscribe half exists in code. `stop()`, `state` and `health()`
stay undeclared until there is teardown to do — the adapter holds no persistent connection of its
own, posting per request.

Doing it in `build_exchange` instead — which already runs `asyncio.run(fetch_instrument_specs(...))`
at composition, "the one read-once moment" — was rejected: it would put a **signed venue write** in
the composition root, and fire §5's refusal before `run_id` binding and observability init, unlike
its `StoreAccountMismatch` twin, which ADR-0043 §10 deliberately placed inside the sequence.

**Ordering, and why each end of it is load-bearing.** The store check stays first (step 2): it is
local, cheap, and can refuse before the engine has done work it would have to unwind. The venue
check and push land at step 4 — after the store is known to be ours, and **before** the step 5
barrier, so the barrier's own `clearinghouseState` read (ADR-0043 §6) observes an already-aligned
venue and the first reconcile cycle cannot manufacture a spurious divergence. Both refusals precede
the barrier, so neither can let an order out.

**That leaves two `clearinghouseState` reads one step apart, and they stay separate deliberately.**
Step 4's read decides the push (§4); step 5's materialises the account row (ADR-0043 §6). Sharing
one payload is *sound* — the row is `accountValue − Σ unrealized_pnl` (ADR-0042 §6), which the push
does not move — but it is not worth buying: the two reads live in different components either side
of a lifecycle boundary, so threading one payload from `Exchange.start()` into the barrier couples
them for the sake of a single info call. Neither read is scarce in the sense §1 is careful about:
both are **unsigned info reads**, and the address-based allowance §1 quotes *"only applies to
actions, not info requests"*. This is also not in tension with §4's rejection of `activeAssetData`:
that was a read scaling **per symbol**, where these are one per boot each, and the second one the
boot already made before this ADR existed.

**The push targets the account the ledger is bound to.** `updateLeverage` is signed through the same
active pool as orders, so when `HyperliquidConfig.vault_address` is set (ADR-0038's sub-account
primitive) the write carries it exactly as a placement does. Without this the engine could align one
account's leverage while trading another's — a silent, total decoupling of the model from reality.

## 8. `updateLeverage` only

The venue-write surface this ADR opens is **exactly one action**. `updateIsolatedMargin
{asset, isBuy, ntli}` and its `topUpIsolatedOnlyMargin` variant are a **named deferred extension
point**, for three reasons:

- **Paper cannot model it.** ADR-0040 §1 fixed paper's isolated collateral as static at open and
  deliberately excluded top-ups and withdrawals. Shipping the write would give live a capability
  with no paper twin, breaking ADR-0034's identical-compute grain in the one place this map has
  been most careful to hold it.
- **There is nothing to push *from*.** Leverage and mode are declarative state that config can
  mirror and a boot-time sync can converge. Isolated collateral is an imperative action with an
  amount and a direction — it has no steady-state value for config to declare.
- **Live already sees its effect.** ADR-0040 §3 ingests the isolated collateral as a position input,
  so a manual top-up is already reflected in reported margin, liquidation price and (per ADR-0041
  §4.1) effective leverage. The surface observes the action without owning it. (**Confirmed by
  [#142](https://github.com/MarcosACH/tickwright/issues/142)**, which drove a real
  `updateIsolatedMargin` of +20 USDC and watched all three move: `margin_used` `25.856067` →
  `45.856067`, `liquidation_price` `52522.4977` → `42395.9154`, `effective_leverage` `5.0119` →
  `2.8260`. The named field was wrong — the collateral is recovered as `marginUsed − unrealizedPnl`,
  not read off `rawUsd`, which measured **negative** — but the argument is unaffected.)

The extension point is reached if and only if the paper model gains dynamic isolated collateral;
exposing it as a `Strategy`-callable position-management action is further still, past this map's
report-only ceiling.

## 9. Bounds are validated on both paths, in `start()`

Every strategy-traded symbol must have an `InstrumentSpec`, and its configured leverage must satisfy
**`1 ≤ leverage ≤ spec.max_leverage`**. A violation raises once, naming every offending symbol with
its bound, and faults — identically on paper and on live.

This spends `InstrumentSpec.max_leverage` on the purpose ADR-0040 §4 introduced it for. Unlike
§3's dead-config rejection — which *is* an `AppConfig` validator, because both its inputs are
`AppConfig` fields — this one cannot be: `max_leverage` lives on the spec, which the **adapter**
authors — from the meta endpoint on live, from `PaperExchangeConfig.instrument_specs` on paper — so
the earliest point at which both paths hold it is `start()` itself. That splits the three checks by
what each one needs: config-only in the validator (§3), config × spec in `start()` (here), and
config × venue in `start()` on live alone (§4/§5).

Leaving it to the venue to reject was rejected: live would fail with a venue error message while
**paper would accept an impossible leverage silently** and compute margin, liquidation price and
effective leverage off it. A strategy would then behave one way in paper and fail at boot on
promotion — precisely the paper/live divergence ADR-0034's identical-compute grain exists to
prevent. Paper validating without writing is not an inconsistency; it is the venue-agnostic half of
the check running where the venue-specific half cannot.

## 10. Post-boot drift: a direct check, because `margin_used` is blind for isolated

Each reconcile compares the ingested `leverage.{type, value}` against config for every held symbol
and emits **`LEVERAGE_DIVERGENCE`** (`leverage.divergence`) **on an exact mismatch**, naming the
symbol, the configured pair and the venue pair. It rides the same alert sink as ADR-0040 §6's
`VALUATION_DIVERGENCE` and is **distinct from it**: that one reports a *computed* number drifting
from the venue's within a tolerance band, this one reports a discrete *operator setting* the engine
declines to re-impose. Exact match, no tolerance band — the pair is discrete, so ADR-0040 §6's
`max(atol, rtol·|venue|)` shape has nothing to measure. Alert-only, never heals, never re-pushes.

**This corrects ADR-0040 §5.** That section claims a live leverage disagreement "surfaces through
the resulting `margin_used` divergence (§6)". It does not, in the mode ADR-0040 §1 calls primary
and §5 makes the default: for an isolated position, a leverage or mode drift produces **no**
`margin_used` divergence at all. The indirect route is blind in the common case, so the direct check
is necessary, not merely sharper.

**(The conclusion is confirmed by [#142](https://github.com/MarcosACH/tickwright/issues/142); its
original premise was wrong and is replaced above.** This section argued the blindness from
ADR-0040 §3 making isolated `margin_used` "≡ the ingested `rawUsd` collateral — the same number on
both sides of the comparison". That premise fails twice: `rawUsd` is not the collateral (it measured
`−103.731933` against a collateral of `25.898067`), and isolated `margin_used` is **not** a static
ingested constant — it is `isolated_collateral + unrealized_pnl` and moves with the mark, so it now
sits *inside* §6's band alongside cross (see the ADR-0040 §3 and §6 corrections).

The blindness is nonetheless real, and for a sturdier reason: **a leverage change never re-margins
an open position** (§5's correction — measured across 5x → 10x → 3x with `marginUsed` unmoved).
Neither term of `isolated_collateral + unrealized_pnl` depends on the leverage *setting* once the
position is open, so a drift in that setting is invisible in `margin_used` no matter which tier the
number belongs to. `LEVERAGE_DIVERGENCE` remains necessary.**)**

It is also nearly free: the reconcile pull already reads `clearinghouseState` for ADR-0040 §3's
liquidation-price read-through, so the comparison costs no additional venue call.

Escalating drift to a fault was rejected. It would hand a venue-side hand-edit the power to kill a
running engine mid-position, and this map's ceiling is report-only: the engine reports the
disagreement and keeps trading, exactly as it reports a negative free margin without acting on it
(ADR-0040 §7).

## Consequences

- **Additive across the board.** `Exchange` gains `start()` (paper's is a validation-only no-op);
  `AppConfig` gains `leverage` plus a cross-field validator rejecting dead entries (§3); `domain`
  gains `LeverageSpec` and the `VenueLeverageMismatch` `InvariantViolation`; ADR-0020's catalog
  gains `LEVERAGE_DIVERGENCE` and `EXCHANGE_LEVERAGE_UNCHANGED`. No seam is broken and no existing
  field is removed.
- **The composition root gains one resolution step** (§2): sparse `AppConfig.leverage` × the
  strategy-declared symbols → a complete `dict[str, LeverageSpec]`, injected into both the
  `PortfolioProjection` and the `Exchange`. Neither consumer resolves defaults itself, so they
  cannot disagree about an unconfigured symbol, and the adapter needs no knowledge of strategies.
- **Amends ADR-0040 §5 twice** — the config block leaves `PaperExchangeConfig` for
  `AppConfig.leverage` (§2), and the `margin_used`-divergence claim is corrected for isolated
  positions (§10). Its "the engine does not set leverage or mode on the venue" sentence is
  superseded by this ADR, as that sentence anticipated.
- **Extends ADR-0024** — step 4 gains the connect half its prose already promised, and the
  barrier-failure policy covers the push (§6) rather than the push getting a policy of its own.
- **Two refusals now guard boot**, in a fixed order: `StoreAccountMismatch` (step 2, "is this my
  ledger?") then `VenueLeverageMismatch` (step 4, "is this the account I am modelling?"). Both
  precede the barrier, so neither can let an order out.
- **The blind write is fresh-as-of-one-read, by choice** (§4). A position opened inside the step-4
  read→write window is re-margined rather than refused — most often by **our own resting order from
  the previous run filling** (ADR-0024 leaves resting `LIVE` orders on the venue across a graceful
  stop, so this is the ordinary case, not an anomaly), and more rarely by foreign flow. The window
  is one startup step, no re-read closes it (`updateLeverage` has no compare-and-set), and #142 may
  close it outright if the venue rejects changes on held positions.
- **The operator keeps the last word in-flight.** A venue-side change during a run stands and is
  alerted; only a deliberate restart re-imposes config. The cost is stated: a divergence can persist
  for the life of a run, and the reported margin and liquidation numbers for a held symbol are then
  the configured view, not the venue's.
- **Deferred, named extension points:** `updateIsolatedMargin` / `topUpIsolatedOnlyMargin` (§8), and
  an `activeAssetData`-based fully-idempotent push (§4).
- **Handed to [#142](https://github.com/MarcosACH/tickwright/issues/142)** — four venue behaviours
  that no documentation or SDK settles, none of which changes a decision above: the response to a
  no-op `updateLeverage` (narrows §6's tolerance); whether changing leverage recomputes an open
  position's `marginUsed`; whether a leverage decrease or mode switch on a held position is
  rejected; and whether `activeAssetData` reports leverage for a symbol with no open position.
  The rate-limit taxonomy is **not** among them: §1 cites the buffer, the accrual and the
  actions-not-info scope directly at the venue's rate-limits page, so nothing there is outstanding.
