# Money type: Decimal everywhere, never float

All prices, sizes, and notionals are `decimal.Decimal`, never `float`. Binary floating point cannot
represent decimal tick/lot grids exactly, and ADR-0017 quantization (round price to tick, size to
lot/step, check min-notional) is precisely the arithmetic that float rounding corrupts — a class of
silent venue-rejection and state-corruption bugs a reference engine must not model wrongly.

Validated against the source: Hyperliquid returns `px`/`sz` as **strings**, so the boundary parses
them **straight to `Decimal`** (exact, no float ever touches the value) and the `serde` codec
(ADR-0025) serializes `Decimal` back **as strings** on the Kafka wire. Latency is an explicit
non-goal (ADR-0020), so `Decimal`'s cost over `float` buys correctness at no price we care about.

Using `float` on the hot path and converting to `Decimal` only at the quantize/submit boundary was
rejected: it reintroduces binary-rounding into every intermediate price computation (strategy
indicators, fill prices, cumulative filled qty) that the engine should keep exact end to end.
