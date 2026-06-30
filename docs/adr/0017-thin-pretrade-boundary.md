# A thin pre-trade boundary (guard + quantization), not a RiskEngine; Portfolio deferred

v1 includes a **thin pre-trade boundary** in the `ExecutionManager`, run before write-ahead/place:

1. **`PreTradeGuard` seam** — min-notional, quantity/price validity, and a **kill-switch** flag.
   Failure → `DENIED` (ADR-0010), never sent. Two impls: a real guard + a `NoopGuard`
   passthrough (tests/paper). Users may plug their own — the Protocol-extensibility story.
2. **Order quantization** — round price to tick and size to lot/step at the boundary, so we never
   emit orders the venue silently rejects (a real state-corruption class).
3. **Minimal instrument specs** (tick, lot/step, min-notional) feed both — from **config** on the
   paper exchange, from the **venue meta endpoint** on Hyperliquid. Deliberately *not* a full
   instrument-provider component; only the fields the guard and quantizer need.

## Deferred (explicit non-goals for v1)

- **Full RiskEngine** — exposure/max-position/aggregate-notional, pre/post-trade portfolio risk.
  Needs positions/PnL, which v1 makes frictionless (ADR-0013).
- **Portfolio / positions tracker** (Open Q#17) — deferred. The engine is order-lifecycle only; a
  `Strategy` keeps whatever position view it needs in its own state. RiskEngine and Portfolio are
  additive seams atop a future accounting layer, not v1 concerns.

This realizes the README's "min-notional/quantity guards at the boundary" pattern and gives
`DENIED` a real source, while staying small.
