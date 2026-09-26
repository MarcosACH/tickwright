# Pre-trade limits: four optional caps on the guard, deny only

A strategy bug can send one huge order or a flood of orders. Before this ADR, nothing in the engine
stopped either. The guard checked only size and price format, min notional, and the kill switch
(ADR-0017). ADR-0017 deferred position limits because the engine had no positions then. ADR-0034
and the accounting surface added positions, so that reason is gone.

We add four optional caps to the real `PreTradeGuard`. Each one is off unless the user sets it.

## Decision

| Cap | Scope | Unit | Denies when |
| --- | --- | --- | --- |
| Max order size | per symbol | coins | the quantized quantity is above the cap |
| Max order value | per symbol | USD | quantity times price is above the cap |
| Max position | per symbol | coins | the worst-case position on the order's side is above the cap, and the order does not move it toward zero without crossing zero |
| Max orders per window | whole engine | placements | the window already holds the maximum |

**(Amended by [#397](https://github.com/MarcosACH/tickwright/issues/397), an order that only
reduces skips max order size and max order value:** before this, a strategy could not close a
position bigger than those caps in one order. It had to split the close, and each part took a rate
cap slot. "Only reduces" uses the worst case from §Max position below. The order reduces when that
worst case gets smaller and does not cross zero. Ending at exactly zero counts as reducing, for a
long and a short alike. Such an order also skips the value cap's mark checks. It passes with a
missing or stale mark. Without that, a mark outage would let a long-only strategy open a position
but not close it. Every other check still applies. That means the kill switch, quantization, min
notional, max position, and the rate cap. An order from a flat position, or one that crosses zero,
gets no exemption. Denial reasons do not change. Reducing orders keep the rate cap. A full close
now takes one order, and the rate cap also protects the account from venue rate limits. The
trade-off is on a thin book. A whole reducing order can fill far below the mark, and the value cap
no longer limits that. The exemption trusts the engine's own position view when the order is sent.
A `reduce_only` flag that the venue enforces would be stronger. It stays deferred (ADR-0030).**)**

**Off unless set.** A symbol with no entry has no per-symbol caps. An unset rate cap never denies.
The user is responsible for reading the docs and setting the caps they need. The engine does not
force a cap on real money.
**(Amended by [#379](https://github.com/MarcosACH/tickwright/issues/379) — an entry must name a
traded symbol:** the engine refuses to start when a symbol entry names a symbol no strategy
trades. Such an entry is nearly always a typo. The symbol the user meant would then run with no
cap. `leverage` refuses the same case (ADR-0044 §3).**)**

**A breach denies that one order.** The order goes to `DENIED` with a reason that names the cap.
Nothing else changes. The kill switch is not tripped, so ADR-0026's "tripped manually only" still
holds. A later order that fits every cap passes.

**Max order value.** A limit order is valued at its quantized limit price. A market order has no
price, so it is valued at the symbol's latest mark (ADR-0039). If the symbol has a value cap and the
mark is missing, or older than its own max age, the market order is denied. The guard cannot prove
the order fits, so it refuses. The max age is its own setting, default 10 seconds. It is not the
reconcile band's `mark_max_age_seconds`, which does a different job. A market order can fill worse
than the mark, so this cap is close for market orders, not exact.
**(Amended by [#391](https://github.com/MarcosACH/tickwright/issues/391) — a sell limit is valued
at the higher of its limit price and the mark:** a limit price is an upper bound for a buy, but only
a lower bound for a sell. A sell limit below the bid crosses on arrival, and a real venue fills it
near the bid. Valued at its own price, a sell of 10 BTC at a limit of 1 checks as 10 USD and fills
near 420,000 USD. So a sell limit is valued at its limit price or the mark, whichever is higher. The
mark rules for market orders apply to it too. With a value cap set, a sell limit is denied when the
mark is missing or older than the max age. Falling back to the limit price was rejected, because it
keeps the gap open. A buy limit is still valued at its limit price and needs no mark. Paper hides
the gap, because the paper exchange fills a crossing limit at its own limit price.**)**

**Max position.** The cap measures the worst-case position on the order's side. That is the
position if every open order on that side fills, and then the new order fills too. For a buy, it is
the net position, plus the unfilled remainder of open buys, plus the new buy. For a sell, it is the
net position, minus the unfilled remainder of open sells, minus the new sell. The order is denied
when the size of that worst case is above the cap. There is one exception. An order that moves this
worst case toward zero always passes, even when the result is still above the cap. This lets a user
reduce a position that is already too big. A position can pass the cap after the cap is lowered, or
after a fill the engine did not place. For example, with a cap of 15, a net of -20, and no open
buys, a new buy of 3 passes. The worst case moves from -20 to -17. A sell that shrinks a long
position can still be denied. For example, with a cap of 15, a net of +5, open sells of 20, and a
new sell of 3, the worst case moves from -15 to -18. The sell is denied. Toward zero means the worst
case gets smaller and does not cross zero. An order that crosses zero gets no exception. It is
checked on the new side, like any other order. For example, with a cap of 15, a net of -20, and no
open buys, a new buy of 36 moves the worst case from -20 to +16. The buy is denied, because 16 is
above the cap. The cap is in coins so a missing mark never blocks it.

**Max orders per window.** N approved placements per S seconds, as a sliding window on the injected
`Clock`. The scope is the whole engine, because a venue rate-limits the account and one process is
one account (ADR-0038). Only approved placements count. Cancels are never blocked and never counted,
because a cancel reduces risk. Denied orders do not count, because they never reach the venue. The
window lives in memory and starts empty on boot. A restart empties the window, so up to N orders
may pass right after a boot. We accept this.

**Check order.** The kill switch comes first, then quantization, then min notional, then the caps.
Min notional applies to limit orders only, because a market order has no price. Every cap applies
to both limit and market orders. The rate cap is checked last, so an order denied by another cap
never takes a slot.

**Limits need the real guard.** If any limit is set and `guard` is `noop`, the engine refuses to
start with a clear error. A limit that is set but not enforced is worse than no limit.
**(Amended by [#379](https://github.com/MarcosACH/tickwright/issues/379) — any limits block
counts:** the engine refuses `noop` whenever the limits differ from the empty default. An empty
symbol entry counts too, even though it sets no cap. The check then needs no edit when a later
slice adds a cap. A hand-kept list of caps could miss one, and that cap would never be
enforced.**)**

**Same checks on paper and live.** The guard stays venue-agnostic. Positions come from the
`PortfolioProjection`, open orders from the `Cache`, and marks from the projection's mark map.

## Considered options

- **Safe defaults that always apply, or required caps on real money.** Rejected. The user owns the
  risk policy, and existing configs keep working unchanged.
- **Trip the kill switch on a breach.** Rejected. That is the automatic circuit breaker ADR-0026
  deferred as risk policy. The position cap already bounds the money at risk.
- **One engine-wide USD cap.** Rejected. Coins carry different risk, so a USD cap per symbol fits.
- **Position cap in USD.** Rejected. It would deny every order on a missing or old mark. Coins are
  exact.
- **Value a market order at the last trade price.** Rejected. One odd trade can print far from fair
  value. The mark is what the engine already values positions with. On paper they are the same
  number.

## Consequences

- The guard is still not a RiskEngine. There is no margin check, no loss limit, no aggregate
  exposure across symbols, and no flatten. Those stay deferred (ADR-0017, ADR-0026).
- The guard now reads positions, open orders, and marks. It already held a clock.
- ADR-0039 rejected a max age on the mark *read path*. This ADR adds one in the guard, which holds a
  clock. See the amendment in ADR-0039.
