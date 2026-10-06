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
- **Each order goes through the single-cancel path.** The manager sets the `cancel_requested`
  marker on each order, with the cancel all's `signal_id`, and checkpoints it before the send.
  An order already marked or already terminal is skipped. So a redelivered cancel all sends
  nothing twice. Reconciliation settles every marked order the same way it does today (ADR-0026).
- **The exchange seam cancels a list.** `cancel(ref)` becomes `cancel(refs)`. A single cancel sends
  a list of one. Hyperliquid has batch cancel actions, so a cancel all is one venue request. A
  batch that holds orders with and without an oid becomes two requests, `cancel` and
  `cancelByCloid`. The paper exchange cancels each ref in turn.
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

- A cancel all that finds nothing to cancel leaves no trace. Its seq can be reused after a
  restart. This is safe, because no durable record carries that id. A single cancel that does
  nothing already behaves this way (ADR-0016).
- Many orders can carry the same `cancel_signal_id`. The seq high-water reads the same max.
- The Hyperliquid adapter needs a reader for one status per cancel. It must also handle one error
  for the whole batch. Like today, an error leaves the marked orders to reconciliation.
- Hyperliquid documents no maximum batch size. The testnet probe in #408 measures it.
- The operator's cancel all (ADR-0052) can use the same `cancel(refs)` call.
