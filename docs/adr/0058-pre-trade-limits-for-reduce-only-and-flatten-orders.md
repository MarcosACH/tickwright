# Pre-trade limits for reduce-only and flatten orders

The pre-trade caps (ADR-0051) were written for orders that can open exposure. A reduce-only order
cannot. The venue shrinks it to the position or rejects it (P1 and P3 in
[#416](https://github.com/MarcosACH/tickwright/issues/416), ADR-0057). A cap that blocks it can only
block a safe exit. This ADR decides which checks still apply to a strategy's reduce-only order and
to a flatten order. Decided in [#435](https://github.com/MarcosACH/tickwright/issues/435), part of
[#408](https://github.com/MarcosACH/tickwright/issues/408). The kill switch part is in ADR-0053 and
ADR-0054.

## Decision

| Check | Strategy reduce-only order | Flatten order |
| --- | --- | --- |
| Kill switch | applies (ADR-0053) | skipped (ADR-0054) |
| Quantization | applies | applies |
| Min notional, limit orders | skipped only for a whole close | not checked, it is a market order |
| Max order size, max order value | skipped | skipped |
| Max position | skipped | skipped |
| Own position does not shrink | denied | not checked |
| Rate cap | applies | skipped |

- **A reduce-only order skips max order size, max order value, and max position.** The venue makes
  sure it cannot grow the account net, so these caps could only stop an exit. It also skips the
  value cap's mark checks. A mark outage cannot block it. A plain order keeps the #397 rule in
  ADR-0051.
- **A strategy's reduce-only order keeps the rate cap.** The rate cap stops a buggy strategy from
  flooding orders. A loop of reduce-only orders is still a flood. The venue rate limit also counts
  every order. A strategy at its cap waits for the window before it can exit.
- **The min notional still applies to a reduce-only order, unless it closes the whole account net.**
  The guard reads the minimum from the symbol's `InstrumentSpec`. On Hyperliquid it is $10.
  Hyperliquid rejects any order under $10 that leaves part of the position open. That holds for
  reduce-only and plain orders, IOC and GTC alike. It accepts an order that covers the whole
  position, and a reduce-only order larger than the position (P9a to P9e in
  [`hyperliquid-min-notional-reduce-only-probes.md`](https://github.com/MarcosACH/tickwright/blob/research/hyperliquid-reduce-only-cancel-market/docs/research/hyperliquid-min-notional-reduce-only-probes.md)).
  So the guard skips the minimum for a reduce-only limit only when its size is at least the size of
  the account net. A plain order keeps today's check, even for a whole close. That is stricter than
  the venue.
- **The guard denies a strategy's reduce-only order unless it shrinks the strategy's own position.**
  Only one strategy trades a symbol (ADR-0038), but the unattributed partition can hold size too.
  The venue checks only the account net. So it can accept an order that flips or grows the
  strategy's own position. Say strategy A is long 1 and a hand buy makes the unattributed partition
  long 1. A reduce-only sell of 2 from A fills, because the account is long 2. The account is flat
  and the books still match it, so reconcile heals nothing. A reads short 1 and may buy to close it.
  That opens real exposure. The same harm comes from the other side. Say A is short 1 and the
  unattributed partition is long 3. A reduce-only sell of 1 from A fills, because it shrinks the
  account net. A then reads short 2. So the guard measures the strategy's own worst case. It is
  built like the max position worst case in ADR-0051, but from the strategy's own position instead
  of the account net. The order passes only when it reduces that worst case, in the #397 sense. The
  worst case must get smaller and must not cross zero. Ending at exactly zero is a full close and
  passes. A strategy with no position has nothing to reduce, so its reduce-only order is denied. All
  open orders on a symbol belong to its one strategy, so the open remainder the guard reads today is
  already the strategy's own.
- **A flatten order skips every cap, the rate cap included.** It is owned by `__operator__` and is
  reduce-only. The guard tells it apart the same way as for the kill switch (ADR-0054). Flatten is
  an operator command, not a strategy bug. Its own pacing bounds it: one order at a time per
  symbol, a 2 second wait after a miss, and a stop after 3 misses (ADR-0056). A rate cap denial
  would fail the exit at the moment it matters most.
- **Denials keep the ADR-0051 shape.** A breach denies that one order, with a reason that names the
  check. The own-position check uses the reason "reduce-only order would not reduce strategy
  position".

## Considered options

- **Treat a reduce-only order like any order that reduces (#397).** The #397 test only guesses from
  the engine's own view. The venue enforces reduce-only, so it can drop the position cap too.
- **Skip the $10 minimum for every reduce-only order.** ADR-0057 said this, based on P7. P7 only
  tested a whole close. The guard would send orders the venue rejects.
- **Shrink an order that would flip the strategy's position.** The guard only denies (ADR-0051). A
  shrink would change the size the strategy asked for without telling it.
- **Deny only an order that crosses zero.** The first draft said this. It misses an order on the
  same side as the strategy's position, which grows it when the unattributed partition is on the
  other side.
- **Drop the own-position check as too rare.** It needs a hand trade on the strategy's symbol. But
  "sell a big size, reduce-only" is a common way to close, and the wrong position it leaves is
  silent.
- **Keep the rate cap for flatten, and retry after a denial.** It adds a wait rule for a case the
  flatten pacing already bounds. An exit run is a fresh process, so its window starts empty anyway.

## Consequences

- `PlaceSignal` needs the `reduce_only` field (ADR-0057 Consequences). `PreTradeReading` needs one
  new field: the strategy's own size in the symbol.
- A strategy closing part of its position under the minimum is denied. It must close the whole
  position, or send at least the minimum. If the unattributed partition is on the same side, the
  strategy cannot cover the whole account net. Its last few dollars then need the operator's
  flatten.
- A reduce-only market order has no price, so the guard cannot check its minimum. A partial one
  under the minimum ends `REJECTED` at the venue. Paper must reject it the same way (ADR-0057,
  corrected).
- Each flatten attempt covers the whole venue position, so the minimum never refuses it. P7 in #416
  shows this for an IOC order, which is what a flatten attempt is (ADR-0056). P9d shows it for a GTC
  order. No other venue check is known to refuse a whole close as too small. ADR-0054 keeps its
  leftover rule for that case as a fallback.
- Venue rate limits for a flatten across many symbols stay open in #408.
