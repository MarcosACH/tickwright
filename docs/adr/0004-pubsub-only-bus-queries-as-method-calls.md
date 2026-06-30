# EventBus is pub/sub only; queries are direct Protocol method calls

The `EventBus` interface is just `publish(event)` / `subscribe(event_type, handler)`. Commands
are modelled as events (e.g. a `Signal`/order-intent is an event the `Exchange` subscribes
to), so there is no separate command pattern. Anything request/response-shaped — reconciliation
reading the exchange's open orders, reading the cache/read-model — is a **direct, synchronous
method call on the relevant Protocol** the caller already holds, not a bus round-trip.

We deliberately omit a request/response bus pattern. Such a pattern exists to
serve many decoupled adapters discovered at runtime; we have a fixed, small set of Protocols a
caller references directly. Req/res also has no native Kafka implementation — it would force a
reply-topic + correlation-id + timeout machinery that makes `InMemoryBus` and `KafkaBus`
diverge exactly where ADR-0001/0002 demand parity, buying nothing a local method call doesn't
already provide.

**Revisit only if** a v1 interaction appears that is genuinely requester-doesn't-know-responder
and must travel across the bus. None exists in the Hyperliquid + paper-exchange scope.
