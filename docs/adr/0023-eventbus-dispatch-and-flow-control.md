# EventBus dispatch is synchronous; backpressure is conflation at the feed, not queues in the bus

The `InMemoryBus` dispatches **synchronously and inline**: `publish` awaits each subscriber
handler in subscription order, with **no per-subscriber queue**. A slow handler throttles
whoever called `publish` (natural backpressure), and `InMemoryBus`'s total order stays a strict
superset of `KafkaBus`'s per-symbol order (ADR-0003) — so the "effectively exactly-once on the
happy path" claim of ADR-0002 holds and the two backends stay parity-locked (ADR-0001). Per-
subscriber `asyncio.Queue`s were rejected: they reintroduce a second concurrency/ordering model
inside the bus (interleaving, queue-full policy, non-determinism) — exactly what ADR-0001 forbids.

Because dispatch is synchronous, the bus itself never needs a flow-control policy. The **only**
async seam that does is the live feed's network ingress.

## Market-data conflation lives at feed ingress (upstream of `publish`)

`HyperliquidFeed` must keep draining its WebSocket (heartbeats/pong) even while a slow
strategy→execution chain is mid-`publish`, so a WS-read coroutine is decoupled from a publish
coroutine by a **bounded, latest-value-per-symbol** buffer. Under backpressure it **conflates** —
keeps the newest tick per symbol, drops stale — and every drop emits a named event
(`feed.lagged` / `tick.conflated`, ADR-0020) so a slow-consumer condition is observable, never
silent. This is sound because `MarketTick` is **last-value-wins**: strategies act on `on_tick`
and the paper exchange re-checks resting limits each tick (ADR-0012), so only the latest price
per symbol ever matters.

Two load-bearing scoping rules:

1. **Conflation is market-data-only.** `Signal`, `ExecutionReport`, and `OrderEvent` are never
   conflated or dropped — losing one corrupts the saga.
2. **Conflation is upstream of `publish`.** It happens inside the feed, before the bus, so both
   backends see the same already-conflated stream. Bus-delivery parity (ADR-0001/0002) is
   untouched. `ReplayFeed` publishes **inline with no conflation** — replay must stay faithful and
   deterministic (it is the tracer and test substrate). An unbounded ingress channel (a common live
   default in event-driven engines) was rejected: unbounded memory under sustained overload with no
   slow-consumer signal.

## Reentrant `publish` drains to quiescence (a FIFO trampoline, not recursion)

A `publish` called from inside a handler appends to **one central FIFO**; the top-level call
drains the whole cascade to quiescence before returning, while a nested `publish` merely enqueues
and returns. Still fully synchronous and single-threaded — the feed's `await publish(tick)` still
returns only once the cascade settles, so the natural backpressure above is preserved. This buys
three things depth-first recursion does not:

- **Order-independence.** Every `MarketTick` subscriber (including the `PaperExchange` caching the
  tick) runs before any `Signal` that tick spawned is processed, so a MARKET order fills against
  the correct latest tick regardless of subscriber registration order.
- **Bounded stack.** Long `tick → signal → order → fill → signal` cascades iterate, they don't
  recurse.
- **Backend parity.** A FIFO breadth-first cascade mirrors `KafkaBus`'s poll-loop (each hop is a
  separate drain step), so `InMemoryBus` can never interleave in an order Kafka could not
  reproduce.

This does **not** contradict the "no internal queue" of the synchronous-dispatch decision above:
that rejected *per-subscriber concurrent* queues. This is one central synchronous FIFO drained
inline — no concurrency, no per-subscriber decoupling, backpressure intact. Different axis.
