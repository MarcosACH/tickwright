# Event schema: frozen dataclasses, typed variants, provenance-free idempotency keys

Events are the system's only currency (CONTEXT.md), so their shape is load-bearing. Four decisions
fix it.

## Representation: frozen dataclasses, serialization at the bus boundary

Domain events are `@dataclass(frozen=True, slots=True)` — immutable ("never mutated after
dispatch"), cheap to construct, **stdlib-only**, trivially readable. The `InMemoryBus` passes them
**by reference with zero serialization**; only `KafkaBus` needs a wire format, so serialization is a
**boundary concern**: a `serde` codec (`msgspec`, fast typed JSON/msgpack) lives at the `KafkaBus`
edge. This keeps "swapping the bus backend changes durability, never the domain type" literally true
— a `Signal` object is identical on both backends, and only `KafkaBus` knows how to encode it.
`pydantic` events were rejected (validation overhead on the hot path buys nothing; events are
engine-constructed and trusted — `pydantic` stays where ADR-0021 put it, config).
`msgspec.Struct`-as-domain-type was rejected as the default: one representation for both paths is
tempting, but it couples every domain type to `msgspec` and blurs the serialization seam this repo
wants explicit.

## Envelope: a small base, with the ordering key as a property

The base `Event` carries `event_id: str` (the dedup key, below), `ts_event: int` (when the fact
occurred, UTC epoch nanoseconds, ADR-0005), and `ts_init: int` (when the object was constructed,
`Clock.timestamp_ns()`). The type tag is the class, not a field (the boundary codec adds a tag).

The bus keys ordering/partitioning on **`event.partition_key`, a property — not on a raw `symbol`
field.** All v1 events are symbol-scoped and return `symbol`, but because the bus reads the property,
a future account-scoped event can override it without touching the bus — honoring ADR-0003's standing
caveat at the cost of one property, not a class hierarchy. **Correlation ids are ambient**
(`ContextVar`, ADR-0020), bound from the event being processed, never duplicated as fields; events
carry only their *domain* identity (`signal_id` on a `Signal`, `cloid` on an `OrderEvent`), which *is*
the correlation source. The `reconciliation: bool` flag (ADR-0011 inv 6) lives on `OrderEvent` only —
ticks and signals are never synthetic.

## Taxonomy: typed variant classes, both layers

Both event layers of ADR-0015 use **typed variant classes** over a single type with a status enum —
for exhaustive `match`/`case`, per-type fields with no optional-field soup, and vocabulary parity with
the reference (ADR-0010's explicit goal).

- Canonical **`OrderEvent` — class-per-transition**, one class per state-entry of the 9-state FSM
  (ADR-0007/0010): `OrderPlaced`, `OrderSubmitted`, `OrderLive`, `OrderPartiallyFilled`,
  `OrderFilled`, `OrderDenied`, `OrderRejected`, `OrderFailed`, `OrderCancelled`. The shared base
  carries `cloid`, `strategy_id`, `signal_id`, `venue_oid: str | None`, `reconciliation: bool`; fills
  add `trade_id`/`qty`/`price`/`cum_qty`; the negative terminals add `reason`.
- Raw **`ExecutionReport`** = **`OrderStatusReport` + `FillReport`** (the established
  reconciliation-report split): status reports carry venue status + `venue_oid` + an optional
  `reason` (venue-supplied detail for a negative status, e.g. a `post_only` rejection; metadata,
  excluded from `event_id`); fill reports carry `trade_id`/`qty`/`price`.
  This also serves the reconciler, which reads open-orders (status) and fill-history (fills)
  separately (ADR-0011 inv 4).
- **`Signal`** = `PlaceSignal` + `CancelSignal` (see ADR-0026).

## Catalog closure: the accounting surface contributes exactly one variant

_Added by ADR-0045 (D12), closing the question ADR-0037 §66 left half-open._

The trade-economics map (ADR-0034–0045) added two variants to this taxonomy and **deliberately no
third**:

- **`FundingAccrual`** `(account, symbol, boundary_ts, amount)` — funding's own event, keyed
  idempotent on `(account, symbol, boundary_ts)` (ADR-0037). It exists because funding is an
  **input with no carrier**: paper *generates* it on a `Clock` cadence and live *ingests* it, so
  something must transport it into the projection.
- **`MarkTick`** `(symbol, mark)` — market data on ADR-0027's reserved additive path (ADR-0039),
  weak-keyed and conflatable like `MarketTick`.

**There is no position or account event, and this is a decision rather than a gap.** A position or
account change is an **output** — a derived consequence of a fill already on this bus, already
keyed `{cloid}:fill:{trade_id}` and already idempotent — where `FundingAccrual` is an input. The
rule the two cases share: **an event carries something the bus does not already carry.** The fee
went the same way for the same reason and needed no variant at all: it rides `OrderFillEvent` as a
read-model (ADR-0036).

Nothing consumes such an event either. ADR-0035 writes Tier-1 **synchronously on the fill-apply
path** rather than by subscription, and ADR-0004/ADR-0041 make every strategy read a pull method
call — so the projection is the writer and the strategy is a puller. Telemetry is served by
ADR-0020's named lifecycle events (`position.opened` / `position.changed` / `position.closed` /
`account.reconciled`), which this surface owes regardless.

Adding one later is an **additive** taxonomy change with a stated trigger — a consumer that cannot
be served by a pull call or a log record, most plausibly an external dashboard reading the Kafka
topic (ADR-0028). See ADR-0045 §1.

## Idempotency keys: deterministic, provenance-free, enforced by the saga

Engine correctness rests on deterministic idempotency keys (ADR-0002). `event_id` is derived per
family:

| Family | `event_id` | Role |
| --- | --- | --- |
| `Signal` | `signal_id` = `{strategy_id}:{symbol}:{seq}` | correctness key (ADR-0006) |
| `FillReport` / `OrderFilled` / `OrderPartiallyFilled` | `{cloid}:fill:{trade_id}` | correctness key |
| other `OrderEvent` / `OrderStatusReport` | `{cloid}:{state}` | single-entry-per-state |
| `MarketTick` | `{symbol}:{ts_event}:{seq}` | **weak** — audit/log only, not a correctness key |

Two load-bearing rules:

1. **Provenance is excluded from the key.** A reconciler-synthesized healed fill and a later
   venue-pushed duplicate of the same fill derive the identical `{cloid}:fill:{trade_id}`, so they
   **collapse** — double-counting is structurally impossible. The `reconciliation` flag is
   audit/provenance metadata only, never part of the key. This is what makes synthetic events (ADR-0011
   inv 6) safe.
2. **Dedup is enforced by idempotent `Order.apply()`, not a global processed-event table.** The saga
   record tracks applied `trade_id`s and current state; `apply(event)` is a no-op if the `event_id` is
   already reflected. This aligns ADR-0002 (idempotent apply) with ADR-0019 (no processed-event-id
   table; in-memory has no redelivery, Kafka rides consumer offsets). A generic `processed_event_id`
   table was rejected — it adds a table the in-memory path never needs and dedups on id rather than on
   domain meaning, where the `cum_qty` invariant already lives.
3. **Ticks get a monotonic gate, not a key.** `MarketTick`'s weak key carries a trailing `seq` — the
   feed's per-symbol source sequence — because the replay path (ADR-0027) is not assumed a real venue
   trade id, so `{symbol}:{ts_event}` alone can collide when several recorded trades share a
   nanosecond; `seq` disambiguates them. It is audit-only as a *key*, but its `seq` component is the
   same monotonic per-symbol discriminator the dedup gate keys on. This weak key is deliberate — but
   the one consumer that is *not* naturally idempotent is `Strategy.on_tick`: a redelivered tick (Kafka
   rebalance, uncommitted tail after crash-restart) double-counts indicator state and can mint a
   *fresh* seq — a new intent no idempotency key catches → duplicate live order. The engine's
   subscription wrapper (ADR-0024) therefore applies a **per-symbol monotonic gate**: a tick whose
   (`ts_event`, `seq`) is ≤ the last dispatched for its symbol is dropped — sound because per-symbol
   ordering (ADR-0003) means a duplicate can only arrive in order, and `seq` (not `trade_id`) is the
   tiebreaker so distinct same-nanosecond trades stay strictly increasing in source order rather than
   in a `trade_id` string order that need not track it — plus a configurable **staleness threshold**
   on live ticks so a restart's redelivered backlog cannot trade on pre-crash prices.
   This makes ADR-0002's "consumers MUST be idempotent" structurally true for strategies on both
   backends, with no strategy-author effort.
