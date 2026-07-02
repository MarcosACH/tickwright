# When to Mock

This repo's architecture makes "never mock our own classes" structural, not aspirational
(ADR-0022): every internal seam has a **real lightweight implementation**, so the default test
wiring is real all the way down:

| Seam          | Production impl        | Test stand-in (real, not a mock) |
| ------------- | ---------------------- | -------------------------------- |
| Event bus     | `KafkaBus`             | `InMemoryBus`                    |
| Durable store | Postgres               | `SQLiteStore(":memory:")`        |
| Exchange      | Hyperliquid            | `PaperExchange`                  |
| Clock         | `LiveClock`            | `ManualClock`                    |
| Market feed   | Hyperliquid WS         | `ReplayFeed`                     |
| Randomness    | OS entropy             | seeded `random.Random`           |

Mock **only** at the true process boundaries:

- The Hyperliquid HTTP/WS transport
- The Kafka client (when exercising the `KafkaBus` path)

Don't mock:

- Our own classes or internal collaborators
- Anything in the table above — the stand-in is a real implementation
- Time or randomness — those are injected, so tests pass `ManualClock` / a seeded RNG instead
  of patching

## Designing for testability: Protocols at seams

Define the seam as a `typing.Protocol` and inject it — don't construct concrete dependencies
internally:

```python
# Easy to swap in tests — dependencies injected, keyword-only
class OrderSaga:
    def __init__(self, *, exchange: Exchange, store: OrderStore, clock: Clock) -> None: ...


# Hard to swap — concrete type and env coupling baked in
class OrderSaga:
    def __init__(self) -> None:
        self._exchange = HyperliquidExchange.from_env()
```

## SDK-style boundary interfaces

At the process boundary, one method per operation with typed shapes — not a stringly-typed
generic fetcher:

```python
# GOOD: each method independently fakeable, types specific per operation
class ExchangeTransport(Protocol):
    async def place_order(self, req: PlaceOrderRequest) -> PlaceOrderResponse: ...
    async def cancel_order(self, cloid: Cloid) -> CancelResponse: ...
    async def open_orders(self) -> list[VenueOrder] | None: ...
    # None = connectivity failure, never [] (ADR-0011)


# BAD: tests must know URL paths and JSON shapes — couples tests to the transport
class HttpFetcher(Protocol):
    async def fetch(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]: ...
```

The SDK approach means each fake returns one specific typed shape, no conditional logic in
test setup, and it's obvious which venue operations a test exercises.

## Fakes over `unittest.mock`

Reach for a hand-written stateful fake first — it records state you assert on through the
public interface:

```python
class FakeTransport:
    """In-memory ExchangeTransport recording placed orders."""

    def __init__(self) -> None:
        self.placed: list[PlaceOrderRequest] = []

    async def place_order(self, req: PlaceOrderRequest) -> PlaceOrderResponse:
        self.placed.append(req)
        return PlaceOrderResponse(status=OrderStatus.RESTING, cloid=req.cloid)
```

Use `unittest.mock` (`Mock(spec=...)`, `AsyncMock`) only for one-off strict-expectation cases
at the true boundary — e.g. asserting the transport is NOT called during a reconciliation
freeze. Always pass `spec=`/`spec_set=` so typos fail loudly.

Heuristic: if the test's main assertion is `assert_called_once_with(...)` on an internal
method, it's testing implementation. Prefer fakes that record state, then assert on behavior
observed through the public interface.
