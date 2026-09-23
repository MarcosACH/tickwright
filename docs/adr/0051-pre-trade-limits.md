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
| Max position | per symbol | coins | the worst-case position on the order's side is above the cap |
| Max orders per window | whole engine | placements | the window already holds the maximum |

**Off unless set.** A symbol with no entry has no per-symbol caps. An unset rate cap never denies.
The user is responsible for reading the docs and setting the caps they need. The engine does not
force a cap on real money.

**A breach denies that one order.** The order goes to `DENIED` with a reason that names the cap.
Nothing else changes. The kill switch is not tripped, so ADR-0026's "tripped manually only" still
holds. A later order that fits every cap passes.

**Max order value.** A limit order is valued at its quantized limit price. A market order has no
price, so it is valued at the symbol's latest mark (ADR-0039). If the symbol has a value cap and the
mark is missing, or older than its own max age, the market order is denied. The guard cannot prove
the order fits, so it refuses. The max age is its own setting, default 10 seconds. It is not the
reconcile band's `mark_max_age_seconds`, which does a different job. A market order can fill worse
than the mark, so this cap is close for market orders, not exact.

**Max position.** The worst-case position on the order's side is the current net position, plus the
unfilled remainder of every open order on the same side, plus the new order. For a sell, the short
side is measured the same way. An order that shrinks the position always passes this cap, because
it moves the worst case toward zero. An order that flips long to short is checked on the new short
side. The cap is in coins so a missing mark never blocks it.

**Max orders per window.** N approved placements per S seconds, as a sliding window on the injected
`Clock`. The scope is the whole engine, because a venue rate-limits the account and one process is
one account (ADR-0038). Only approved placements count. Cancels are never blocked and never counted,
because a cancel reduces risk. Denied orders do not count, because they never reach the venue. The
window lives in memory and starts empty on boot. The window is seconds long, and so is a restart.

**Check order.** The kill switch comes first, then quantization, then min notional, then the caps.
The rate cap is checked last, so an order denied by another cap never takes a slot.

**Limits need the real guard.** If any limit is set and `guard` is `noop`, the engine refuses to
start with a clear error. A limit that is set but not enforced is worse than no limit.

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
