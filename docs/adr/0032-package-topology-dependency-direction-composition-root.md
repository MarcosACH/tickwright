# Package topology, dependency direction, and composition root

"Each addition its own module, decoupled from the others" needs **enforceable structural
invariants**, not just Protocol seams. This ADR fixes them. The concrete folder tree is a
`/module-map` artifact; what is load-bearing (and ADR-worthy) is the dependency direction, the
extension unit, and the wiring mechanism. Bound by the README non-goal: **no plugin system,
registries, or config-DSLs — extensibility is implementing a Protocol.**

## Dependency direction (inward, ports-and-adapters)

- **`domain`** at the center — events, the seam **Protocols**, value/identity types, order-FSM
  types. Depends on nothing.
- **Concrete impls** (venue adapters, bus/store backends, the paper stack, strategies) depend on
  **`domain` only**.
- **`engine`** (`ExecutionManager`/saga, reconciliation, recovery, the runner) depends on `domain`
  **Protocols**, **never** on a concrete impl.
- **No adapter imports another adapter; core never imports an adapter.**

This is the rule that keeps additions decoupled as the repo grows. It is **enforced mechanically**
with an `import-linter` contract in CI — the same test-assertable-contract spirit as the named-event
catalog (ADR-0020): a cross-adapter or core→adapter import **fails the build**, so "decoupled" is a
gate, not an aspiration.

## The venue adapter is the extension unit

Each real venue is **one package** co-locating its `MarketFeed` + `Exchange` + instrument-spec
sourcing + `*Config` (extending ADR-0031's self-contained-adapter rule to include the feed, since a
venue's feed and exchange share venue knowledge — symbol/asset mapping, endpoints, auth — and
splitting them across seam-first `feeds/` and `exchanges/` trees would duplicate it). Non-venue
reference impls (`PaperExchange`, `ReplayFeed`, `InMemoryBus`, `SQLiteStore`) group **by seam near
their Protocol**, not under the venue-adapters package — they are local/test stacks, not venues.

**Rejected: seam-first packaging** (`feeds/hyperliquid/` + `exchanges/hyperliquid/` in separate
trees, the README's original hypothesis) — fragments a single venue's knowledge across two packages.
Exact names are deferred to `/module-map`; the **principle (venue = one module)** is fixed here.

## Composition root: one explicit builder, `match`-based selection

A single composition root — `build_engine(config) -> Engine` — reads the typed `*Config` objects
(ADR-0021) and constructs the concrete impls; the `Engine` receives them **already built** and never
imports a concrete class. Impl selection is an **explicit `match`** over a small config discriminant
(`exchange: "hyperliquid" | "paper"`, `bus: "in_memory" | "kafka"`, …). Adding an impl adds **one
`match` arm at the top of the app** — touching no adapter and no engine internal; the adapter stays
ignorant of the core.

**Rejected:** (a) an importable-config / entry-point **registry** (the established
runtime-pluggability model) — buys pluggability the README explicitly refuses, and hides wiring
behind import-path strings; (b)
the **`Engine` constructing its own dependencies** — couples the core to concrete impls and breaks
the dependency direction above. A readable two-arm `match` at the composition root is the honest
minimal wiring: exactly one place knows all concretes, and it sits at the top of the graph.

## Consequences

- Adding a venue touches four localized places: a new adapter package, a new `*Config`, one
  composition-root `match` arm, and deployment — all at the graph's edges, none in the core.
- `import-linter` boundaries are part of the CI gate, so an accidental coupling is caught at review.
- The `domain` package is the stable contract every impl and the engine compile against; changing an
  adapter can never ripple inward.
