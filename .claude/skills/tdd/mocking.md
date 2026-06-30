# When to Mock

Mock at **system boundaries** only:

- External APIs (payment, email, etc.)
- Databases (often — prefer a real test DB via `sqlx::test` / `testcontainers` when feasible)
- Time/randomness (inject a `Clock` / `Rng` trait)
- File system (sometimes — `tempfile` is often better than mocking)

Don't mock:

- Your own structs/modules
- Internal collaborators
- Anything you control end-to-end

## Designing for Mockability

At system boundaries, design traits that are easy to fake or mock:

**1. Inject collaborators via traits**

Pass external dependencies in rather than constructing them internally:

```rust
// Easy to swap in tests
async fn process_payment<P: PaymentClient>(order: &Order, client: &P) -> Result<Receipt> {
    client.charge(order.total_cents()).await
}

// Hard to swap — concrete type baked in, env coupling
async fn process_payment(order: &Order) -> Result<Receipt> {
    let client = StripeClient::from_env();
    client.charge(order.total_cents()).await
}
```

**Choosing the injection shape:**

| Shape | When to use |
|-------|-------------|
| `<P: PaymentClient>` (generic) | Default. Zero-cost dispatch, monomorphized. |
| `&dyn PaymentClient` | Heterogeneous collections, plugin-style code, smaller binary. |
| `Box<dyn PaymentClient>` | Owned dyn storage in a long-lived struct. |

**2. Prefer SDK-style traits over a generic fetcher**

One method per external operation, not one stringly-typed `fetch`:

```rust
// GOOD: each method is independently mockable, types are specific
#[cfg_attr(test, mockall::automock)]
#[async_trait]
pub trait UserApi {
    async fn get_user(&self, id: UserId) -> Result<User, ApiErr>;
    async fn get_orders(&self, user_id: UserId) -> Result<Vec<Order>, ApiErr>;
    async fn create_order(&self, data: NewOrder) -> Result<Order, ApiErr>;
}

// BAD: tests must know URL paths and JSON shapes — couples tests to transport
#[async_trait]
pub trait HttpFetcher {
    async fn fetch(&self, endpoint: &str, body: serde_json::Value) -> Result<serde_json::Value, ApiErr>;
}
```

The SDK approach means:

- Each fake/mock returns one specific typed shape
- No conditional logic in test setup
- Easy to see which endpoints a test exercises
- Type safety per endpoint

## Fakes vs `mockall`

Reach for a hand-written fake first when the dependency is stateful or reused across tests:

```rust
#[derive(Default, Clone)]
struct FakePayments {
    charges: Arc<Mutex<Vec<u64>>>,
}

#[async_trait]
impl PaymentClient for FakePayments {
    async fn charge(&self, cents: u64) -> Result<TxId, PayErr> {
        self.charges.lock().unwrap().push(cents);
        Ok(TxId::from("fake"))
    }
}
```

Use `mockall` for one-off, strict-expectation cases:

```rust
#[automock]
#[async_trait]
pub trait PaymentClient {
    async fn charge(&self, cents: u64) -> Result<TxId, PayErr>;
}

// in a test
let mut m = MockPaymentClient::new();
m.expect_charge()
    .with(eq(1_000))
    .returning(|_| Ok(TxId::from("tx_1")));
```

Heuristic: if the test asserts on `times(N)` or `expect_X` for an internal method, it's probably testing implementation. Prefer fakes that record state, then assert on behavior observed through the public interface.

## Async traits in 2025+

- `async fn` in traits is stable for static dispatch — you often don't need `#[async_trait]` for generic-only traits.
- For `dyn Trait` with async methods, you still need `#[async_trait]` or `trait-variant`. Keep it where required, drop it where not.
