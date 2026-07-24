# One account per process: the account is a deployment fact, declared by the venue adapter

_Accepted via the D5 grilling session on decision ticket [#118](https://github.com/MarcosACH/tickwright/issues/118), part of the trade-economics map [#107](https://github.com/MarcosACH/tickwright/issues/107). Answers ADR-0034's "requires multi-account" consequence; builds on ADR-0031 (one venue per process), ADR-0034 (D1) and ADR-0035 (D2)._

An `Engine` trades **exactly one account, on exactly one venue**. The account is a **deployment fact**, like the venue before it (ADR-0031): scaling to N accounts is N processes, not one engine fanning out over accounts. The account's identity and netting semantics are **declared by the venue adapter** and exposed through a new `AccountSpec` on the `Exchange` Protocol — the same adapter-authored / `Exchange`-exposed / `Engine`-wired shape ADR-0031 fixed for `InstrumentSpec`.

**Rejected: N accounts as a runtime dimension inside one process** (a list of accounts in `AppConfig`, an `Exchange` client and reconcile cycle per account, a strategy declaring which account it trades). It reintroduces, one level down, precisely what ADR-0031 rejected for venues: per-account reconciliation loops inside one process, account-qualified identity, and a routing table in the `ExecutionManager`. The cost we accept is stated plainly below.

## The account is ambient: identity yes, key no

The account has an identity, and that identity is **not** a key component.

- `account_id` lives on the `Account` aggregate, is stamped on durable ledger rows, on the `FundingAccrual` event (ADR-0037) and on telemetry (ADR-0020).
- In-memory `Position` is keyed `(strategy, symbol)` — the account component is constant within a process, so paying for it at every lookup would be a dead dimension.

ADR-0034/0035/0037 describe `Position` as per-`(account, strategy, symbol)` and the funding key as `(account, symbol, boundary_ts)`. Those tuples stand as the **logical** keys and remain correct; this ADR records that in a one-account process the leading component is ambient rather than materialized. This is ADR-0003's own discipline — a scope fact "must not be forced onto the bare-symbol key", while `partition_key` stays a *property* so the identity can exist without being the key — applied one level up.

**Why account differs from venue here.** ADR-0031 removed venue from the model entirely, and the account is not symmetric with it: the venue has no runtime object (it *is* the adapter), whereas `Account` is a modelled, stored `domain` aggregate reconciled against the venue's account snapshot (ADR-0035). An aggregate needs an identity, and durable state that names its own account is self-describing rather than unambiguous-by-deployment-convention (ADR-0009/0025).

## `AccountSpec`: the venue-declared account facts

The `Exchange` Protocol gains one synchronous accessor, `account_spec() -> AccountSpec`, peer of `instrument_specs()`. `AccountSpec` is a frozen `domain` value carrying the venue's **static declarations** about the account it trades:

- `account_id` — the qualified identity (below);
- `netting` — the `NET` / `HEDGE` semantics ADR-0034 requires every adapter to declare.

Collateral currency and cross-vs-isolated margin mode join it additively as the margin/mark tickets land.

**One value, not accessor-per-fact.** Adding a field with a default to a frozen dataclass breaks no implementation; adding a Protocol method breaks every one — including the user-supplied `Exchange` implementations the extensibility story invites (ADR-0031). The naming mirrors the existing pair: `InstrumentSpec` is to `Instrument` as `AccountSpec` is to `Account`, so `Account` stays the live aggregate.

**Rejected: the adapter returns a seeded `Account`.** It conflates a static, synchronous, read-once-at-composition declaration with the venue's *dynamic* balance truth, which is an async read that can fail and is governed by ADR-0011 invariant 1 (`None` → freeze, never flat). Composition would either have to await a failable call or accept an `Account` with meaningless zeroed balances.

### The identity is qualified

`account_id` is composed by the adapter from **venue + network + the venue-native identifier** — `hyperliquid-testnet-0xABC…`, `paper-<label>`. A bare identifier is ambiguous: Hyperliquid uses **the same wallet address on mainnet and testnet** (the network is selected by the endpoint, not the address), so a bare-address id would let an operator flip `testnet` while reusing the store and carry a testnet-shaped ledger onto a mainnet account with every check passing. Paper and live are likewise not one namespace.

On Hyperliquid the venue-native identifier is the resolved trading address — `vault_address or account_address or wallet.address` — which is already what every `/info` read and WS user subscription is keyed by. The id the ledger is keyed by is therefore the same address the venue keys its truth by.

## Strategies do not name their account

`StrategyConfig` gains nothing: every registered strategy trades the process's one account. ADR-0034's rule — `(strategy, symbol)` ownership **disjoint per account** — reads in a one-account process as **disjoint process-wide**, enforced in the `StrategyHost` registry: it fail-fasts today on a duplicate `strategy_id` (ADR-0018), and **gains** a second fail-fast on a symbol another registered strategy already owns. That gate does not exist yet — it lands with the accounting surface, listed under Consequences below. In v1 it is unconditional, both shipped adapters being `NET`; `HEDGE` adapters may relax it (ADR-0034).

A binding field would have exactly one legal value and would add a way to misconfigure something that cannot otherwise be wrong.

## Account exclusivity (invariant)

**An account is owned by exactly one engine process.**

ADR-0011 gives *orders* an ownership boundary: the engine manages only orders it placed, recognized by its own cloid, and an order at the venue with an unrecognized cloid is logged as external and never acted on. **That boundary does not extend to accounting.** D1 made the account-level net aggregate the sole reconciliation anchor, reconciled against `szi` / `accountValue` — one account-wide number with no cloid on it — and Tier-1 divergence is *healed*, exactly at venue precision (ADR-0034). A second engine on the same account therefore does not merely confuse the ledger: it causes the reconciler to emit synthetic fills that absorb flow this engine never placed.

This binds tighter under the one-account-per-process rule than it would have otherwise, because the answer to "run two strategies on the same symbol" is now "deploy a second process" — and the obvious wrong way to do that is to point both processes at one account. **Same-symbol isolation requires a second _account_, not merely a second process** (ADR-0034/0018).

Enforcement matches what is observable:

- The `Store` records the `account_id` its ledger belongs to, **binding a ledger to one account**: a restart where the adapter reports a different account **fail-fasts** rather than accreting one account's fills onto another's ledger. With the qualified id above, this catches a network flip as well as an address change. It is a binding check, not a lock — it says nothing about who else is trading that account concurrently.
- **A second engine on the same account is undetectable in-process — on this host or any other, whatever its store.** No arrangement of two engines on one account trips the binding check: with the per-process stores ADR-0028's instance isolation mandates, both ledgers name that account and the check passes on each; sharing one store passes too, since both adapters report the same `account_id`. The check fires only when a ledger and its adapter name **different** accounts — ADR-0028's shared-store collision, or a network/address flip across a restart. Concurrent ownership therefore stays a documented invariant, and ADR-0028's instance-isolation rule (topic, consumer group, store location are per-process) extends from per-venue to **per-venue-and-account**.

## Foreign flow lands in an unattributed partition

Exclusivity is an invariant, not a guarantee: a human can trade the account in the venue UI, a position can pre-exist first startup, collateral can be transferred. The per-strategy overlay is therefore keyed by `strategy_id: str | None`, where **`None` is the reserved unattributed partition** — never registrable, so no strategy can be handed it — which absorbs any residual between the reconciled account net and the sum of strategy positions.

ADR-0034's bridging invariant **Σ(per-strategy signed size per symbol) = account net size = venue `szi`** thereby holds *by construction*, and the residual becomes one inspectable number rather than a silent failure of the invariant. The alternative — leaving the anchor to heal while the overlay stands still — creates pressure to attribute foreign flow to whichever strategy owns the symbol, which would both corrupt that strategy's PnL and let its close-my-position logic act on exposure it never opened. The synthetic heal needs a price to book against either way (ADR-0034), so the partition costs a key, not a mechanism.

## Reaching a second account on Hyperliquid

The isolation primitive ADR-0034 requires must actually be addressable. Hyperliquid sub-accounts and vaults **have no private key** — the venue is explicit: *"Subaccounts and vaults do not have private keys. To perform actions on behalf of a subaccount or vault signing should be done by the master account and the vaultAddress field should be set to the address of the subaccount or vault"*, that field being *"its Onchain address in 42-character hexadecimal format"* ([exchange endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint)). The master key signs and the sub-account address travels in the action; because `/info` reads and WS user subscriptions are unsigned reads keyed **by address** (R1, [#108](https://github.com/MarcosACH/tickwright/issues/108)), that same onchain address is also the read address. Today `exchange.py` passes `None` into the `active_pool` slot of `sign_l1_action(wallet, action, active_pool, nonce, expires_after, is_mainnet)` (SDK 0.24.0) and sends no `vaultAddress`, so a sub-account is unreachable.

`HyperliquidConfig` therefore gains **`vault_address`** (env-only like its siblings, ADR-0021): passed as `active_pool` when signing, sent as `vaultAddress` in the exchange payload, and used as the read address. It is a *distinct* venue mechanism from the existing `account_address`, which serves agent-wallet delegation where the signature itself implies the master and no extra field is sent — hence a second field rather than one overloaded address plus a mode flag. Venue-faithful naming: `vaultAddress` is the wire field, and the venue documents it for sub-accounts as well as vaults.

## Consequences

- **The deployment tax is the price.** Two strategies on one symbol means two processes: two stores, two bus namespaces (ADR-0028), two feed connections, and — on Hyperliquid — a sub-account. On the shipped venue this is the *only* isolation available: Hyperliquid positions are `oneWay`, so ADR-0034's `HEDGE` relaxation is not reachable there.
- **Standing caveat (mirroring ADR-0031's).** If one process ever hosts N accounts, then: `AccountSpec` becomes per-`Exchange`-instance rather than per-process; `Position` and the funding key must materialize their account component (the logical keys already name it); the reconcile cadence and `Account` aggregate fan out per account; `StrategyConfig` gains the binding this ADR declines; and the paper venue grows independent collateral pools. It **must not** be reached by qualifying the bare-symbol ordering/dedup key (ADR-0003/0025).
- **ADR-0003's account-scope caveat is resolved.** It anticipated account-scoped events needing their own ordering key. `FundingAccrual` (ADR-0037) is that event, and it carries account identity as a *property* while staying symbol-partitioned — the outcome ADR-0003 called for.
- **The paper venue models one collateral pool per process**, and `PaperExchangeConfig` supplies the label its qualified id is built from. Starting-collateral configuration remains its own ticket.
- **Deferred to implementation:** the `AccountSpec` type and accessor, the `StrategyHost` symbol-ownership fail-fast (ADR-0034's disjointness rule, unenforced today), the `strategy_id: str | None` overlay key, the `Store` account-identity binding check, and `HyperliquidConfig.vault_address` (plus its `.env.example` entry) land as one vertical slice. Nothing in this ADR is enforced in code until it does.
