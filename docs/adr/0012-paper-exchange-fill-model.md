# Paper-exchange fill model: deterministic default + seeded-stochastic second impl

The `PaperExchange` subscribes to `Signal`s and `MarketTick`s, holds a book of resting LIMIT
orders, fills MARKET on receipt against the latest tick, and re-checks resting limits each tick.

**Order types: `MARKET` + `LIMIT` only** (the two fundamentals all others derive from; stops,
trailing, if-touched deferred). **TIF: `GTC` + `IOC`** plus a **`post_only`** flag (reject if it
would cross); `FOK`/`GTD`/`DAY` deferred.

The fill behavior is a seam with **two implementations, no more**:

- **`ImmediateFillModel` (default).** Deterministic, optimistic, zero-setup: MARKET fills now
  at the tick price; LIMIT fills when a tick touches/crosses its price, **at the limit price**;
  unlimited liquidity → **full fill, no partials, zero slippage, zero latency**. No RNG at all —
  the reproducible star of the MVP. Slippage is a config knob, off by default.
- **`StochasticFillModel` (second impl).** A **seeded** RNG behind the same interface:
  `prob_fill_on_limit` (queue-position proxy), `prob_slippage` (one-tick), configurable
  **partial fills**, and **latency** via the injected `Clock` (ADR-0005). Seeded ⇒ deterministic
  per seed; mirrors the standard probabilistic `FillModel` approach.

Both the RNG and the `Clock` are injected, so every fill path is deterministic in tests
(boundary-mock-randomness, ADR-0005). This **keeps the `PARTIALLY_FILLED` state** of ADR-0007:
the `StochasticFillModel` is what exercises it; the default never partials.

Default is zero-slippage/optimistic for maximum readability and reproducibility — realism is
the explicit purpose of the second model, not a default tax.

## Crash recovery: the paper book re-adopts from the store

The paper book is in-process and volatile, so restart semantics are pinned: at `start()` the
`Engine` hands the `PaperExchange` the store's **non-terminal orders** (mirroring the spec-wiring
path, ADR-0031) and it **rebuilds its resting book by cloid**. Startup reconciliation (ADR-0011)
then finds them present — exactly like a live venue that kept them — so restart **re-adopts**
resting orders instead of ghosting every healthy order, and the crash-recovery E2E (ADR-0022)
exercises the same re-adopt path the live venue exercises, deterministically.
