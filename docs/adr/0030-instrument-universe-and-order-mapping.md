# Instrument universe and order mapping: perps only, MARKET is an aggressive IOC in the adapter

Validated against the Hyperliquid exchange-endpoint docs (order action schema, TIF, asset indexing).

## v1 trades Hyperliquid perpetuals only

The instrument universe is **perpetuals** — Hyperliquid's flagship, indexed directly in the meta
`universe`. Spot (asset index `10000 + spot_index`, `MAX_DECIMALS = 8` vs perps' `6`) is deferred as
an additive `InstrumentSpec` extension (ADR-0017), not a v1 concern. "Perps" here means only the perp
trades feed (ADR-0027) and perp order placement; the frictionless engine (ADR-0013) models no margin,
funding, or liquidation regardless. The `InstrumentSpec` carries the asset index plus `szDecimals`/`MAX_DECIMALS`
from the meta endpoint, feeding the sig-fig price rule (ADR-0017); supporting spot later is the
`10000+` offset and `MAX_DECIMALS = 8`.

## `reduce_only` is deferred

`PlaceSignal` stays exactly as ADR-0026: `side`, `qty`, `price`, `order_type` (MARKET/LIMIT), `tif`
(GTC/IOC), `post_only`. Hyperliquid exposes a `reduce_only` (`r`) flag, but it is **position**
semantics the engine explicitly defers (ADR-0013/0017) and is **inert on the `PaperExchange`** (the
v1 default target has no positions). Adding a flag that does nothing on the default path and is
unmodeled by the engine is scope creep; it is a trivial passthrough to add when a positions surface
arrives.

## MARKET maps to an aggressive IOC limit — in the thin adapter, not the engine

Hyperliquid has **no native market order**: execution types are limit orders with TIF **ALO**
(post-only), **IOC**, or **GTC** (plus trigger orders). The engine keeps its clean model (MARKET/LIMIT
× GTC/IOC × `post_only`, ADR-0012); the venue quirk lives entirely in the thin `HyperliquidExchange`
adapter (ADR-0015):

- **MARKET** → an **IOC limit at an aggressive price**: `latest_price × (1 + slippage_bound)` for buy,
  `× (1 − slippage_bound)` for sell (a configurable bound with an SDK-style default), then quantized
  per the ADR-0017 price rule (perps allow ≤5 sig figs, integer prices always).
- **`post_only`** → TIF **ALO** (rejects if it would cross — matches our `post_only` semantics).
- **LIMIT** → limit with TIF **GTC** or **IOC**.

Surfacing the slippage bound as an engine-level `PlaceSignal` field was rejected: it leaks a
venue-specific execution detail into the order model the `PaperExchange` never needs. On the paper
path MARKET remains a direct fill at the latest tick (ADR-0027); the aggressive-IOC translation is a
live-adapter concern only.
