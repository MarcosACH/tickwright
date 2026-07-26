# Economic terminology and the closed event catalog: gross realized PnL, unsigned notional, and no position event

_Accepted via the D12 grilling session on decision ticket [#138](https://github.com/MarcosACH/tickwright/issues/138), the terminal ticket of the trade-economics map [#107](https://github.com/MarcosACH/tickwright/issues/107). Delivers the economic vocabulary **ADR-0034's Consequences terminology bullet** and **ADR-0035's Naming section** deferred to "its owning model tickets plus a terminology sweep", and **closes ADR-0025's event catalog** for the accounting surface — the half **ADR-0037's Consequences "Event schema" bullet** left open when it settled funding. **Amends ADR-0025** (the catalog gains a closure clause, not a variant), **ADR-0040** (amended three times — §7's equity invariant is stated on the formula that does not survive a live heal, §2's `margin_used` row loses its `(initial)` heading, and its ADR-0044 amendment block loses the word "computed"), **ADR-0020** (four roadmap names) and **ADR-0044 §2** (its `Margin` gloss). Corrects five documentation defects the sweep surfaced._

Every mechanism in this surface is now decided: the truth model (ADR-0034), the topology
(ADR-0035), fees (ADR-0036), funding (ADR-0037), the account (ADR-0038), the mark (ADR-0039),
margin and liquidation (ADR-0040), the read seam (ADR-0041), genesis (ADR-0042), durability
(ADR-0043) and the venue leverage write (ADR-0044). What was left is the vocabulary those eleven
documents use to describe the same quantities — deliberately deferred, because a definition written
before the mechanics landed would have drifted from them — and the one schema question none of them
answered: whether a position or account change is a bus event.

This ADR settles both, and records the five places where the existing documents already disagree
with each other.

## 1. The catalog closes: a position or account change is not a bus event

**Decision: no `PositionChanged`, no `AccountState`, no sibling of `FundingAccrual`. ADR-0025's
event catalog is closed for this surface** — stated as a closure, so a future reader finds a
decision rather than an omission.

The argument is that no consumer exists, and none is being withheld:

- **The projection is the writer, not a subscriber.** ADR-0035 applies Tier-1 **synchronously on
  the `ExecutionManager` fill-apply path** — explicitly *not* a fill-bus subscriber. The one
  component that would care about a position change is the one that causes it.
- **Strategies pull.** ADR-0004 makes queries method calls; ADR-0041 gives strategies
  `position()` / `open_positions()` / `account()`. A strategy never subscribes to PnL, by decision,
  and ADR-0039 already refused the smaller version of this question (no `on_mark` callback).

So the event would ship with **zero consumers** — a seam with zero implementations, against the
map's ≤2 bar, which asks for at most two and is not satisfied by none.

**Funding is the instructive contrast, and it cuts the other way.** ADR-0037 made funding an event
because it is an **input with no carrier**: something must transport paper's generated accrual from
the `Exchange` into the projection, and it needs a durable idempotency key
(`(account, symbol, boundary_ts)`) to survive catch-up, reconcile and restart. A position change is
an **output** — a derived consequence of a fill that is *already* on the bus, already keyed
(`{cloid}:fill:{trade_id}`), already idempotent. Publishing it re-publishes derived state, which
ADR-0002's machinery would then have to dedup for no gain. The rule the two cases share, stated
once: **an event carries something the bus does not already carry.**

**Rejected: publish it for external consumption.** Under `TICKWRIGHT_BUS=kafka` (ADR-0028) a bus
event is externally consumable by a dashboard, where a named event reaches the outside as a log
record. This is the only real cost of the decision and it is accepted, because taking it would put
the same fact on two channels that can drift, and would spend the per-symbol ordering key
(ADR-0003), the topic, and the write-integrity story (ADR-0028) on a consumer that does not exist
yet. If one ever does, the additive path is the one ADR-0027 reserved for `MarkTick` — a new variant
in the taxonomy — and it is cheaper to add then than to maintain an unconsumed event until then.

**Rejected: an `AccountState` snapshot event.** A recurring account snapshot on the bus is how an
event-fed portfolio keeps itself current and how it persists. Neither job is open here: ADR-0035
feeds the projection synchronously and ADR-0043 persists it as current-state rows. What would remain
is a periodic broadcast of numbers any reader can pull, most of them Tier-2 and therefore
*recomputed per read* (ADR-0034) — an event whose payload is stale the moment it is serialized.

## 2. Telemetry rides the named-event catalog

§1 rests on telemetry being served, so this ADR names what serves it. ADR-0020 makes named
lifecycle events first-class — structured records with the correlation id bound, a closed
`NamedEvent` enum, and the standing requirement that **"every component state change emits a named
event"**. A position change is exactly that, so this surface owes named events *regardless* of the
§1 decision; §1 only decides that it owes nothing further.

Added to ADR-0020's **roadmap** list (not the shipped enum — ADR-0020 grows that "one slice at a
time: a name lands only with its emitting path and a catalog-walk test"):

| Name | Emitted when |
| --- | --- |
| `position.opened` | a fill takes a `(strategy, symbol)` record from flat to non-flat |
| `position.changed` | a fill or accrual moves a non-flat record |
| `position.closed` | a fill returns a record to `size = 0` |
| `account.reconciled` | a ledger reconcile cycle completes (its divergences already have their own alert types — ADR-0040 §6's `VALUATION_DIVERGENCE`, ADR-0044 §10's `LEVERAGE_DIVERGENCE`) |

A flip through zero (P1, [#119](https://github.com/MarcosACH/tickwright/issues/119)) is one fill
that closes and reopens; it emits `position.closed` then `position.opened`, because the residual
opens a **fresh** average-cost record and telemetry that hid that would misreport the entry basis.

## 3. Realized PnL is gross — and normalizing to gross belongs to the adapter

**Decision: realized PnL is trade-only — gross of fees and gross of funding.** Fees (ADR-0036) and
funding (ADR-0037) accrue on their **own Tier-1 ledger lines** and are never folded into it.

This is not the mainstream convention, which nets fees into a position's realized PnL, and it is not
free of consequence, so it is recorded rather than assumed:

- **Netting would double-count.** `equity` subtracts fees at the cash line (§5, ADR-0042 §4). A
  realized line that had already absorbed them would charge every fee twice.
- **The three lines are separately reconciled.** ADR-0034 Tier-1 is exact at venue precision with
  heal-and-alert on *each* accumulated line. Merged lines cannot be independently checked, and a
  fee bug would surface as a realized-PnL divergence — an alert pointing at the wrong quantity.
- **Attribution needs them separate.** ADR-0041 §4.1 carries `realized_pnl`, `fees` and `funding` as
  distinct fields on the own-attribution slice precisely so a strategy can see what its edge was
  before costs.

**A venue's own convention never reaches `domain`.** Venues differ on whether their reported
realized figure bundles the fee, and at least one is ambiguous in its own documentation. That is an
**adapter** concern: the `Exchange` seam **reports a gross realized figure**, un-bundling whatever
its venue bundles, exactly as ADR-0036 made it the seam that computes-or-reads the fee and ADR-0037
the seam that generates-or-ingests funding. `domain` and `engine` never learn that a venue had a
convention at all. This is what keeps the definition venue-agnostic rather than Hyperliquid-shaped,
and it means a second venue with the opposite convention is an adapter detail, not a vocabulary
change.

**Consequence for the live reconcile:** whatever venue quantity the live Tier-1 realized line is
compared against must be normalized to gross **before** the comparison, or ADR-0034's zero-tolerance
check would diverge on every fill. Pinning the Hyperliquid arithmetic empirically — whether
`closedPnl` includes the fee, and whether an opening fill carries a non-zero `closedPnl` — is added
to [#142](https://github.com/MarcosACH/tickwright/issues/142)'s scope, alongside the effective-leverage
denominator ADR-0041 §4.1 already sent there. It fixes one adapter's arithmetic; it cannot change
this definition.

**Rejected: a `Total PnL` term** (`realized + unrealized`). No field carries it — ADR-0041 exposes
the two separately — and a term with no field behind it drifts. Recorded as a non-term.

## 4. Notional is unsigned, and the account total is gross

**Decision: `notional = |szi| × mark` — unsigned — and the account total is the sum of those
absolute values, i.e. *gross* notional, not net exposure.** ADR-0040 §2 already computes it this
way; this fixes the word.

The hazard is concrete and observed in engines of this shape: the same word ends up meaning
*unsigned* at position grain and *signed* at portfolio grain, so a hedged book's total silently
becomes net exposure while every per-position reader still sees a magnitude. A strategy long 3 BTC
in one symbol and short 3 BTC in another would then read a total notional of `0` and conclude it has
no exposure, when it holds two positions that are each independently liquidatable — the numbers
`notional` exists to feed (`margin_used`, `maintenance_margin`, `effective_leverage`) are all
magnitudes, and all three would be nonsense computed off a netted total.

***Net exposure* is recorded as a non-term.** Not because the quantity is meaningless — it is the
right input to a portfolio-risk surface — but because that surface is the deferred RiskEngine
(ADR-0017), outside this map, and minting the word here would give a reader two plausible readings
of one field.

## 5. Equity: one invariant, and the accrual rule beneath it

**Decision: `equity = cash + Σ unrealized_pnl` is the canonical definition.** The four-input
expansion is a separate statement about how `cash` accrues:

> `cash` accrues from four signed inputs — genesis, `+` realized PnL, `−` fees, `+` funding
> (ADR-0042 §4) — with **one standing exception on live**: ADR-0034's reconcile heal corrects it
> toward venue truth without accruing from anything the engine did.

**This corrects ADR-0040 §7**, which attached the phrase *"one invariant, both paths"* to the
expanded form:

```
equity = genesis_collateral + Σ realized_pnl − Σ fees + Σ funding + Σ unrealized_pnl
```

That expression holds **only while `cash` equals the sum of its four inputs**, and ADR-0042 §4 says
plainly that on live it need not: the synthetic cash adjustment "exists precisely because the four
inputs above failed to reproduce the venue's number." So on the live path — the path §7 names — the
formula is false the instant a heal fires, while `equity = cash + Σ uPnL` remains true. The
substantive rules are untouched: the fee term still subtracts, free margin is still
`equity − total_margin_used`, and a negative free margin is still reported without consequence.

Two further reasons the compact form is the right canon:

- **`AccountView` exposes `cash` and `equity` as separate fields** (ADR-0041 §4). A reader holding
  both needs the identity relating them, and no document stated it plainly.
- **It is the form the reconciler checks.** ADR-0040 §2 tables `equity` as the Tier-2 anchor
  computed as `cash + Σ unrealized_pnl`; the canon now matches the computation.

**`Equity` stays account-grain only.** ADR-0041 §4.1's isolated `effective_leverage` denominator
(`isolated_collateral + unrealized_pnl`) is described as the position's backing collateral plus its
unrealized PnL, and **not** named "position equity" — a second grain for a word this glossary
already binds to the account pool is how `Portfolio` became a flagged ambiguity.

## 6. `margin_used` is not "initial margin"

**Decision: drop the "initial" label.** `margin_used` is **the collateral a held position ties up**;
`initial margin` moves to the `_Avoid_` list as an order-admission concept this surface does not
implement.

"Initial margin" conventionally names the collateral reserved **when an order is submitted** — a
pre-trade admission gate. This surface has no such gate: the map's ceiling is *reported* margin, and
ADR-0040 §7 is explicit that the paper exchange never rejects for margin and never liquidates. The
two quantities coincide numerically at open on a cross position and diverge immediately afterwards,
which is exactly the kind of coincidence that makes a wrong name survive review. Carrying the label
invites a reader fluent in the convention to conclude that orders are margin-checked — the single
most consequential thing this surface does **not** do.

**Five places are corrected to match** — `ADR-0040 §2`'s table heading, plus every use of the
phrase to name the *amount* rather than a venue's fraction: `ADR-0040 §1` and the sentence
`ADR-0043 §3` quotes from it, both of which now read "the amount moved in at open"; `ADR-0040
§4`'s `initial_margin = notional / leverage`, which now reads "the amount is
`notional / leverage`"; and `ADR-0035`'s `margin_init` aside, which now names it "the collateral
moved in at open".

That last one is `ADR-0040 §1`'s wording verbatim, and the *at open* scoping is load-bearing: "the
collateral a position ties up" is this glossary's definition of `margin_used`, so asserting that
equals `notional / leverage` would restate §9.1's cross-only defect.

What stands is **fraction** language only — an adapter may use its venue's words internally,
`domain` uses this one. That leaves four phrases in this repo's own prose, three of them in
`ADR-0040 §4`: its "initial-margin fraction is `1/leverage`" and "per-venue initial-margin haircut"
(the same sentence whose *amount* clause is corrected above), its quoted venue rule "half the
initial margin at max leverage", and `ADR-0041 §4.1`'s "initial-margin fraction". A fifth
occurrence sits outside the test entirely: `ADR-0044`'s citation of the venue's margining page,
§"Initial Margin and Leverage" — an external document's own heading, quoted as a proper noun. The
distinction is the test to apply to any future occurrence: **a fraction may keep the name, an
amount may not.**

## 7. `Collateral` is a flagged ambiguity, not a term

**Decision: no `Collateral` glossary term.** This overrides ADR-0034's _Consequences_ terminology
bullet, which listed one.

The word already carries **three distinct, separately-owned meanings** in this repo:

| Sense | Owner |
| --- | --- |
| the account's collateral **pool** | `Account`, `Equity` & `Free margin` |
| an isolated position's **locked** collateral | `Margin`, `Leverage` & `Margin mode` |
| the account's **opening cash line** | `Genesis collateral` |

A generic entry would be a **fourth** definition overlapping all three and requiring sync with each
— the duplication the docs-sync policy calls a bug. `CONTEXT.md`'s own format prescribes the remedy:
genuine multi-sense conflicts go in **Flagged ambiguities** with a resolution, which is where
`Portfolio`'s two senses were already settled. One entry, three pointers, no fourth definition.

## 8. What lands in `CONTEXT.md`

- **`Realized PnL` & `Unrealized PnL`** — one combined entry. They are defined by contrast, and the
  gross rule (§3) belongs to both; stated twice it would drift. The glossary's existing combined
  entries (`Equity` & `Free margin`, `Leverage` & `Margin mode`, `PositionView` & `AccountView`)
  set the precedent.
- **`Notional`** — its own entry (§4), the shared base of `margin_used`, `maintenance_margin` and
  `effective_leverage`, which currently each restate it.
- **No `Mark price` entry** — `MarkTick` already defines it ("the venue's robust-median valuation
  price"), and a second entry is a copy that drifts. `MarkTick`'s opening line is sharpened so the
  phrase resolves there.
- **`Collateral`** → **Flagged ambiguities** (§7).
- **Non-terms**, recorded so they are rejected on sight rather than re-proposed: `Total PnL` (§3),
  *net exposure* (§4), `initial margin` (§6).

## 9. Sweep corrections

Five places where the documents already disagreed. None changes a decision; all are drift the sweep
exists to catch. (**Two of the five — defects 2 and 5 — were later withdrawn** by
[#142](https://github.com/MarcosACH/tickwright/issues/142): the disagreement was real in both
cases, but the sweep resolved it toward the landed ADRs when `CONTEXT.md` held the accurate
reading. Each is annotated in place below.)

1. **`CONTEXT.md` `Margin` states `margin_used = notional / leverage` unconditionally.** True for
   **cross** only. An isolated position's `margin_used` is computed from its locked collateral —
   ingested on live, static-at-open on paper (ADR-0040 §1/§3).
2. **`CONTEXT.md` `Margin` calls the whole term "recomputed each read (Tier-2)".** This one the
   sweep got **wrong**, and [#142](https://github.com/MarcosACH/tickwright/issues/142) reversed it —
   `CONTEXT.md` was right and the ADRs it was corrected against were not. See the correction below.

**(Defects 1 and 2 amended by [#142](https://github.com/MarcosACH/tickwright/issues/142).** The
sweep faithfully propagated an error that was already in the landed ADRs, so it inherited rather
than introduced it — but the entry as written is wrong and would mislead.

Measured against a funded testnet position, the venue's isolated `marginUsed` is
`isolated_collateral + unrealized_pnl` and **moves with the mark** (`25.860067` at mark 64796 →
`25.856067` at mark 64794). So:

- **Defect 2 is withdrawn.** Isolated `margin_used` is **Tier-2, recomputed each read** — exactly
  what `CONTEXT.md` said. What is Tier-1 and persisted is the underlying **`isolated_collateral`**,
  which is what ADR-0043 §3 actually puts on the row; the sweep conflated the two because ADR-0040
  §3 and ADR-0041 §6 did. Both are now corrected at source, and `CONTEXT.md`'s `Margin` term is
  restored to "recomputed each read" for both modes rather than being "fixed" toward the error.
- **Defect 1 survives, minus its field name.** `margin_used` is still not `notional / leverage` for
  an isolated position, so the unconditional formula is still wrong. But the collateral is **not**
  `rawUsd` — that measured `−103.731933` against a collateral of `25.898067`, being the cash leg net
  of cost basis. It is recovered as `marginUsed − unrealizedPnl`.

This is the sweep's own thesis holding at one remove: the drift it exists to catch was, again,
between *landed* documents — and here the glossary was the accurate one.**)**
3. **`CONTEXT.md` `PositionView` flattens ADR-0041 §6's nullability rule** to "`None` when the mark
   is absent", dropping the per-**term** refinement: a field is `None` only when the mark is absent
   *and its own terms need it*. A flat position's valuations need no mark and read `0`.
4. **`CONTEXT.md` `Equity` and `Genesis collateral` contradicted each other** on whether anything
   but the four inputs moves `cash` — `Genesis collateral` already described the live heal that
   `Equity`'s formula implicitly denied. Resolved by §5.
5. **The `margin_used`-is-computed gloss, in the three places that carry it.** `ADR-0044 §2`'s
   naming argument called `Margin` "a computed Tier-2 output"; the sweep read that as defects 1 and
   2 from the other side, in the ADR that reasons *about* the glossary. That sentence is
   **duplicated**, so correcting it at its origin alone would have left the copies to drift:
   `ADR-0040`'s ADR-0044 amendment block restates the naming argument verbatim ("binds **Margin**
   to the computed collateral a position ties up"), and `CONTEXT.md`'s `Leverage` entry re-glosses
   the cross-reference in its own `_Avoid_` list ("the *computed* collateral a position ties up") —
   **below**, in the same file, the `Margin` entry that calls an isolated position's `margin_used`
   "an ingested input rather than a computed valuation", so the two disagreed in the document a
   reader consults for the word itself. The **three-way sync is the durable part** of this entry;
   the gloss it synced them onto is not — [#142](https://github.com/MarcosACH/tickwright/issues/142)
   withdrew it, below. The naming argument itself is unaffected throughout — it turns on `Margin`
   naming a **reported output** rather than an operator input, true in both modes — so only the
   gloss ever changed.

**(Defect 5 withdrawn by [#142](https://github.com/MarcosACH/tickwright/issues/142), for the same
reason as defect 2.** This entry judged "a computed Tier-2 output" wrong on the premise that an
isolated position's `margin_used` is neither computed nor Tier-2 — the landed-ADR error defects 1
and 2 inherited. Measured, it is **both**: `isolated_collateral + unrealized_pnl`, recomputed each
read, mark-dependent. So `ADR-0044 §2`'s original gloss was accurate and the computed/ingested split
this sweep installed in its place is the drift.

All three copies are corrected again, and this time toward the mode-neutral reading: `margin_used`
is **computed in both modes** — off the nominal leverage on a cross position, off the ingested
collateral on an isolated one. `ADR-0040`'s amendment block keeps "reported collateral" as the
mode-neutral word rather than as a correction of "computed".

The three-way sync stands as this entry's real finding: a gloss duplicated across three documents
was moved in step twice, and would have drifted both times had the copies not been named here.**)**

## Consequences

- **ADR-0025 is amended** with a closure clause: the event catalog is complete for the accounting
  surface, and the accounting surface contributes exactly one variant — `FundingAccrual`. A future
  position event is an additive taxonomy change with a stated trigger (§1), not a gap.
- **ADR-0040 is amended three times**: §7's equity invariant is restated on the compact form (§5);
  §2's `margin_used` row loses its `(initial)` heading (§6) — which also costs §1, §4, the
  sentence ADR-0043 §3 quotes from §1, and ADR-0035's `margin_init` aside the words "the initial
  margin", wherever they named the *amount*; and its ADR-0044 amendment block loses the word
  "computed" from the `Margin` gloss it restates (§9.5 — the word is *not* restored by §9.5's #142
  withdrawal; "reported" simply stops being a correction and becomes the mode-neutral choice). Its
  numbers are unchanged.
- **The `Margin` gloss is corrected in all three documents carrying it** (§9.5) — `ADR-0044 §2`'s
  "a computed Tier-2 output" becomes the computed/ingested split, and the copies in `ADR-0040`'s
  amendment block and `CONTEXT.md`'s `Leverage` entry follow it. No naming decision moves.
  (**Amended by [#142](https://github.com/MarcosACH/tickwright/issues/142)**: the split is withdrawn
  — `margin_used` is computed in both modes — and all three copies move again, in step. What this
  bullet records that survives is the *sync*, not the gloss it synced them onto.)
- **ADR-0020 gains four roadmap names** (§2), none of them shipped until its emitting path is.
- **The ADR-0034 / ADR-0035 / ADR-0037 deferrals are delivered**, each annotated in place so a
  reader landing on any of the three finds the pointer rather than an open question. The economic
  vocabulary those documents postponed now exists, with one deliberate deviation: `Collateral` is a
  flagged ambiguity rather than a term (§7).
- **ADR-0036/0037's decisions are confirmed, not amended** — ADR-0037's two annotations mark its
  deferrals closed and change nothing it decided. Their "own ledger line, never folded into realized
  PnL" treatment is what §3 names and generalizes; the new content is that **normalizing a venue's
  realized figure to gross is the adapter's job**, which extends their existing seam rather than
  moving it.
- **[#142](https://github.com/MarcosACH/tickwright/issues/142) gains one item** (§3): pin whether
  Hyperliquid's `closedPnl` includes the fee, and whether an opening fill carries a non-zero
  `closedPnl`. It tunes an adapter, and cannot reopen §3.
- **The map's frontier is empty of decisions.** With this ticket closed, [#107](https://github.com/MarcosACH/tickwright/issues/107)
  holds only the `wayfinder:task` #142, and the destination — the PRD — is reachable via `/to-spec`.
