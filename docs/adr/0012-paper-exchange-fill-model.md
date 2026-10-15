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

**(Amended by #484 — paper reads the orders itself, and the row keeps their terms:**
The `Engine` hands paper nothing. `app/build.py` injects a `resting_orders` read through
`paper_store_reads`, next to `account_net` and `applied_fills`. Paper calls it in `start()`, which
runs before the barrier. The order row keeps the price, time in force, `post_only` and
`reduce_only` the venue got, so paper can rebuild each order at the size still open. A row written
before those columns existed has no price. Paper leaves it out, and reconcile ghosts it.
Paper rebuilds only `LIVE` and `PARTIALLY_FILLED` GTC rows. Those are the orders the venue confirmed
it holds. A `PENDING` or `SUBMITTED` row may never have reached paper. Rebuilding it would invent an
order nobody placed, so paper leaves it out and reconcile resolves it `FAILED`.
The read hands paper each saga, not a bare venue order. Paper records the saga's state as the
order's status, so `fetch_order` finds it and reconcile never ghosts it. Paper also numbers the
order's next fill after the fills the saga booked. A reused trade id would be dropped as a
duplicate.
On Kafka, a fill paper published just before a crash is redelivered after the rebuild. The saga had
not booked it, so the rebuilt order still counts its size. Paper subscribes to `FillReport` and
books such a fill on the rebuilt order. Every event is keyed by symbol, so the fill arrives before
any new tick on that symbol. Paper reads the fill number from the trade id. It books only a number
past its count, so its own fills and fills the saga already booked are never booked twice.
**)**
