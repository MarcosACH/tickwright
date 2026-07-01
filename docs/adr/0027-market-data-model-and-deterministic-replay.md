# Market data: MarketTick is a last-trade TradeTick from Hyperliquid `trades`; JSONL replay drives the clock

`MarketTick` is the only market input in v1 (CONTEXT.md), so its shape and source are load-bearing.
Validated against the Hyperliquid WebSocket docs and the established market-data type taxonomy.

## MarketTick is a last-trade tick, sourced from the `trades` channel

ADR-0012 already committed to a **single-price, zero-slippage** fill model ("MARKET fills at the tick
price; LIMIT fills when a tick touches/crosses its price"), and the glossary frames `MarketTick` as a
"latest price" and says to **avoid "quote."** Both point at a last-trade tick (a `TradeTick`),
not a top-of-book quote (`QuoteTick`). So `MarketTick` maps 1:1 from Hyperliquid's `trades`
(`WsTrade`) payload:

| `MarketTick` | Hyperliquid `trades` | Type |
| --- | --- | --- |
| `symbol` | `coin` | `str` |
| `price` | `px` | `Decimal` |
| `size` | `sz` | `Decimal` |
| `aggressor_side` | `side` | enum (trade aggressor side) |
| `trade_id` | `tid` | `str` |
| `ts_event` | `time` (ms→ns) | `int` |

The live `HyperliquidFeed` subscribes `{"type": "trades", "coin": <symbol>}` per symbol.

**Paper `ImmediateFillModel`, now concretely specified** (ADR-0012 unchanged): MARKET fills at the
latest `MarketTick.price` (zero slippage); LIMIT **buy** fills when an incoming `tick.price ≤
limit_px` (at `limit_px`), LIMIT **sell** when `tick.price ≥ limit_px`; `post_only` rejects if the
latest tick already crosses at submission. A single price needs no book side, consistent with the
existing zero-slippage model.

**Refinement to ADR-0025:** the weak `MarketTick` dedup key becomes `{symbol}:{tid}` on the live path
(a real venue trade id, more robust than `{symbol}:{ts_event}`), and `{symbol}:{ts_event}:{seq}` on
`ReplayFeed`. Still weak (ticks are not a correctness key).

**Rejected:** `bbo`/`QuoteTick` — reintroduces the "quote" the glossary avoids and needs two prices
against the single-price model; `allMids` — mid is not a tradeable price and has no per-symbol channel
for symbol-keyed conflation (ADR-0023); `l2Book`/`candle` — outside the "only ticks" v1 scope.

**Tick-only is a scope choice, not a foreclosure.** `MarketTick` is the sole v1 market input, but the
door stays open: additional *live* data types a strategy cannot self-derive (top-of-book `QuoteTick`,
funding rate, mark price, L2 book) are an **additive** path — a new typed event variant (ADR-0025)
plus an additive, default-no-op `Strategy` callback — needing no rework of the existing type or the
`on_tick` seam. v1 needs none of them (frictionless ⇒ no funding; single-price fill ⇒ no quotes).
**Timeframes/bars are deliberately not an engine concern:** ticks are the atomic,
timeframe-independent stream, and a strategy wanting bars aggregates them inside its own state
(ADR-0016) — the engine stays honestly tick-driven, with no `BarAggregator` component.

## Deterministic replay: JSONL rows, feed drives the ManualClock

`ReplayFeed` reads newline-delimited JSON rows (one `MarketTick` per line — readable, diffable,
matching the JSON observability ethos of ADR-0020) and, for each row, **advances the injected
`ManualClock` to the row's `ts_event` before publishing**. Replay is therefore deterministic *in
time*: reconciliation loops, retry backoff, and paper-latency timers fire deterministically relative
to the replayed ticks, and tests never sleep (ADR-0005, ADR-0022). `LiveClock` ignores this (real
wall-clock time). `ReplayFeed` never conflates (ADR-0023) — replay must stay faithful. Having the
test harness advance the clock separately was rejected: it couples every replay test to manual clock
bookkeeping and risks feed/clock drift.
