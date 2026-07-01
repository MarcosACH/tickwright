# Deterministic signal_id, engine-derived cloid, engine-side saga is the dedup authority

Every `Signal` carries a strategy-assigned `signal_id = {strategy_id}:{symbol}:{seq}`, where
`seq` is the strategy's monotonic intent counter restored from its snapshot on restart. The
engine derives the Hyperliquid client order id (`cloid`, a 128-bit hex string) deterministically
from `signal_id`, checkpoints the order saga keyed by `signal_id`, and on any re-seen
`signal_id` **resumes** the saga rather than placing a second order. The engine is the sole
dedup authority — Hyperliquid documents a `cloid` field and `cancelByCloid` but does **not**
document duplicate-cloid rejection, so venue-side dedup is never assumed (a bonus if present).

The key must survive two distinct replays: bus redelivery of the same event (dedupe on event
id) and strategy *recomputation* after restart (the re-emitted signal must carry the same id).
The second is why the **load-bearing invariant** is: *signal ids are a pure function of
strategy state and stable across restart — never random.* Random/engine-generated ids make
idempotent recovery impossible. Content-hashing order fields was rejected: two legitimately
identical intents (e.g. two equal safety orders at one price) would collide and silently drop a
real order.

## Consequences

- Strategy authors owe one contract, documented loudly in `extending.md`: **signal ids must be
  deterministic and replayable.** Non-negotiable — it is the price of idempotent recovery.
- The `cloid` doubles as the stable handle for cancel-by-cloid and reconciliation matching.
- The saga records the **venue order id** (Hyperliquid `oid`) alongside the `cloid` once the
  venue assigns it — tracking both `client_order_id` and
  `venue_order_id`. Either id can drive cancel/reconcile (Hyperliquid supports cancel by cloid
  or by oid).
- The `{symbol}` component is unqualified by venue because Tickwright runs **one venue per process**
  (ADR-0031); multiplexing venues in one process would require venue-qualified identity and must not
  overload the bare-symbol key (see ADR-0003's venue-scope caveat).
