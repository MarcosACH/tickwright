# How a strategy cancels all its orders

A strategy can cancel one order by the `signal_id` it emitted (ADR-0026). It cannot list its own
open orders, so it has no way to cancel all of them. This ADR decides how it asks the engine to do
it. Decided in [#415](https://github.com/MarcosACH/tickwright/issues/415), part of
[#408](https://github.com/MarcosACH/tickwright/issues/408).

## Decision

- **A new signal type, `CancelAllSignal`.** It sits next to `PlaceSignal` and `CancelSignal`. It
  carries its own seq'd `signal_id` and no target. The emitter gets `cancel_all(symbol=...)`, which
  returns that id.
- **One signal covers one symbol.** The bus orders signals per symbol, and the symbol is the
  partition key. So a cancel all on a symbol always arrives after every place the strategy sent
  earlier on that symbol. To clear every symbol, the strategy sends one signal per symbol.
- **It cancels only orders sent before it.** The `ExecutionManager` takes the strategy's open
  orders on that symbol whose seq is lower than the cancel all's seq. The seq only goes up, across
  all of a strategy's symbols, so "lower seq" means "sent earlier". The result never depends on
  delivery timing. A redelivered or replayed cancel all can never cancel an order sent after it.
  One gap remains, for a cancel all sent but not handled before a crash. See Consequences.
- **Its seq is durable once handled.** Before it marks anything, the manager writes the cancel
  all's seq to a per-strategy seq record in the store. The ADR-0016 high-water fold reads that
  record too. So a restart never reuses the seq of a handled cancel all, even one that found
  nothing to cancel.
  Without this, a restart could give a new order a lower seq than an old cancel all still on the
  Kafka topic. A redelivery of that cancel all would then cancel the new order.
  The write only raises the record. It keeps the higher of the stored seq and the new one. Each
  symbol has its own partition, so an older cancel all on one symbol can arrive after a newer one
  on another. If it lowered the record, a restart could reuse the newer seq.
- **Each order goes through the single-cancel path.** The manager sets the `cancel_requested`
  marker on each order, with the cancel all's `signal_id`, and checkpoints it before the send.
  A terminal order is skipped. An order whose marker already has this seq or a higher one is
  skipped too. So a redelivered cancel all sends nothing twice. An order marked by an earlier
  cancel takes the new marker and is sent again. That earlier cancel may never have reached the
  venue, after a crash in the send window or a venue error. So a new cancel all is how a strategy
  retries. Reconciliation settles every marked order the same way it does today (ADR-0026).
- **The exchange seam cancels a list.** `cancel(ref)` becomes `cancel(refs)`. A single cancel sends
  a list of one. Hyperliquid has batch cancel actions, so a cancel all is one venue request. A
  batch that holds orders with and without an oid becomes two requests, `cancel` and
  `cancelByCloid`. The paper exchange cancels each ref in turn.
  **(Extended by the [safe exit module map](../module-maps/safe-exit.md),
  [#457](https://github.com/MarcosACH/tickwright/issues/457):** `cancel` takes a list of
  `OrderRef | ExternalOrderRef`. Operator cancel all also cancels hand-placed orders, which have an
  oid and no cloid of ours (ADR-0052). `ExternalOrderRef` holds a symbol and a required oid, and
  Hyperliquid cancels it with `cancel`. `OrderRef` keeps its required cloid. So `fetch_order` and
  every reconcile read stay keyed by cloid, and a ref with neither key cannot be built.**)**
- **The kill switch does not block it.** The guard only checks places (ADR-0053).
- **No "all done" event.** The strategy gets one `OrderCancelled`, or a fill, per order.

## Considered options

- **Make `target_signal_id` optional on `CancelSignal`.** A missing field would quietly mean
  "cancel everything". With real money, that is too easy to send by mistake.
- **Let the emitter remember the ids it placed and send one `CancelSignal` each.** It loses that
  memory on restart, and it misses orders still in flight.
- **One signal for every symbol.** It must travel on one partition. On Kafka, a place sent earlier
  on another symbol can arrive after it, and that order would survive.
- **Cancel every open order at the time the signal is handled.** Today this gives the same result,
  because of per-symbol ordering. But its meaning would depend on delivery timing.
- **Keep `cancel(ref)` and add `cancel_many(refs)`.** Each adapter would have two ways to do the
  same thing, and they could drift.

## Consequences

- The store gets one new per-strategy seq record, on SQLite and on Postgres.
- The record covers a cancel all the manager handled, not one the strategy only sent. On Kafka, a
  sent cancel all can still be on the topic at a crash. The restart's high-water fold does not
  wait for it, so the strategy can get a seq at or below it. Its first handling is still safe.
  Places sent after it on that symbol are behind it in the partition. But a second redelivery,
  after one of those places was handled, would cancel an order sent after it. Places already have
  this gap, because the ADR-0016 fold reads handled seqs, not sent ones. Writing the record when
  the strategy sends would close it. That would give `SignalEmitter` the store, which is a larger
  change. Tracked under "Not yet specified" on #408.
  **(Corrected:** the gap hits places too, so it is out of scope for #408. It is now the bug
  [#442](https://github.com/MarcosACH/tickwright/issues/442).**)**
- A single cancel that marks nothing still leaves no trace, and a restart can reuse its seq. That
  stays safe, because it names one fixed target. A reused seq cannot change what it cancels.
- Many orders can carry the same `cancel_signal_id`. The seq high-water reads the same max.
- The Hyperliquid adapter needs a reader for one status per cancel. It must also handle one error
  for the whole batch. An error leaves every order in the batch marked and still resting.
  Reconciliation settles an order only once it leaves the venue. It never sends a cancel. So the
  order rests until the strategy sends another cancel all, or the operator runs theirs.
- A single `CancelSignal` on an order that is already marked still does nothing. Only a cancel
  all sends again.
- Hyperliquid documents no maximum batch size. The testnet probe in #408 measures it.
  **(Resolved in [#416](https://github.com/MarcosACH/tickwright/issues/416):** at most 200
  cancels fit in one action. At 201 the venue refuses the whole action, with no status per order.
  So the Hyperliquid adapter must split a longer list. Evidence: P4 in
  [`hyperliquid-reduce-only-cancel-market-probes.md`](../research/hyperliquid-reduce-only-cancel-market-probes.md).**)**
- The operator's cancel all (ADR-0052) can use the same `cancel(refs)` call.
