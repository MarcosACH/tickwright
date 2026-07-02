# A thin pre-trade boundary (guard + quantization), not a RiskEngine; Portfolio deferred

v1 includes a **thin pre-trade boundary** in the `ExecutionManager`, run before write-ahead/place:

1. **`PreTradeGuard` seam** — min-notional, quantity/price validity, and a **kill-switch** flag.
   Failure → `DENIED` (ADR-0010), never sent. Two impls: a real guard + a `NoopGuard`
   passthrough (tests/paper). Users may plug their own — the Protocol-extensibility story.
2. **Order quantization** — rule-based rounding at the boundary, so we never emit orders the venue
   silently rejects (a real state-corruption class). Directions are pinned: **size rounds down** to
   `sz_decimals` (never exceed the strategy's intent; a size that rounds to zero → `DENIED`), and
   **price rounds toward the passive side** (buy down, sell up).
3. **Minimal instrument specs** feed both: `sz_decimals`, `max_decimals`, optional `max_sig_figs`,
   and `min_notional` — from **config** on the paper exchange, from the **venue meta endpoint** on
   Hyperliquid. A static tick size was rejected — Hyperliquid has **no fixed tick**: a price is
   valid iff it has ≤5 significant figures and ≤ `max_decimals − sz_decimals` decimal places
   (integer prices always valid), so granularity depends on price magnitude. One shared quantizer
   implements the sig-figs ∧ decimals rule; a plain decimal-places grid (`max_sig_figs` absent) is
   the degenerate case the paper config expresses. Deliberately *not* a full instrument-provider
   component; only the fields the guard and quantizer need.

The specs are **sourced by the `Exchange` adapter** (which owns venue knowledge), exposed via the
`Exchange` Protocol, and wired into the guard/quantizer by the `Engine` at startup — keeping the
guard venue-agnostic. This sourcing/wiring path is specified in ADR-0031.

## Deferred (explicit non-goals for v1)

- **Full RiskEngine** — exposure/max-position/aggregate-notional, pre/post-trade portfolio risk.
  Needs positions/PnL, which v1 makes frictionless (ADR-0013).
- **Portfolio / positions tracker** (Open Q#17) — deferred. The engine is order-lifecycle only; a
  `Strategy` keeps whatever position view it needs in its own state. RiskEngine and Portfolio are
  additive seams atop a future accounting layer, not v1 concerns.

This realizes the README's "min-notional/quantity guards at the boundary" pattern and gives
`DENIED` a real source, while staying small.
