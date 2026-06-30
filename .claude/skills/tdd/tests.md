# Good and Bad Tests

## Good Tests

**Integration-style**: Test through real interfaces, not mocks of internal parts.

```rust
// GOOD: Tests observable behavior
#[tokio::test]
async fn user_can_checkout_with_valid_cart() {
    let checkout = Checkout::new(FakePayments::ok(), FakeMailer::default());
    let mut cart = Cart::new();
    cart.add(product());

    let receipt = checkout.checkout(&cart, payment_method()).await.unwrap();

    assert_eq!(receipt.status, Status::Confirmed);
}
```

Characteristics:

- Tests behavior callers care about
- Uses public API only (no `pub(crate)` shortcuts, no `#[cfg(test)] pub` escape hatches)
- Survives internal refactors
- Describes WHAT, not HOW
- One logical assertion per test

Notes on Rust specifics:

- Prefer `tests/` integration tests for end-to-end behavior — they can only see your crate's public API, which forces good interface boundaries
- Keep `#[cfg(test)] mod tests { ... }` for unit tests only when behavior is genuinely internal (e.g., a parser combinator that isn't exposed)
- Returning `anyhow::Result<()>` from a test lets you use `?` instead of unwrapping, but don't hide assertion failures behind `?`

## Bad Tests

**Implementation-detail tests**: Coupled to internal structure.

```rust
// BAD: Tests implementation details
#[tokio::test]
async fn checkout_calls_payment_client_charge() {
    let mut mock = MockPaymentClient::new();
    mock.expect_charge()
        .with(eq(cart.total_cents()))
        .times(1)
        .returning(|_| Ok(TxId::from("tx_1")));

    checkout(&cart, &mock).await.unwrap();
    // No assertion on observable outcome — only that a method was called.
}
```

Red flags:

- Mocking internal collaborators
- `expect_xxx().times(N)` on internal methods (asserting call counts/order)
- `pub(crate)` or `#[cfg(test)] pub` exposed only so a test can poke internals
- Test breaks when refactoring without behavior change
- Test name describes HOW not WHAT
- Verifying through external means (DB row, log lines, file bytes) instead of the interface

```rust
// BAD: Bypasses interface to verify
#[tokio::test]
async fn create_user_saves_to_database() {
    create_user(NewUser { name: "Alice".into() }).await.unwrap();

    let row: (String,) = sqlx::query_as("SELECT name FROM users WHERE name = $1")
        .bind("Alice")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(row.0, "Alice");
}

// GOOD: Verifies through interface
#[tokio::test]
async fn create_user_makes_user_retrievable() {
    let user = create_user(NewUser { name: "Alice".into() }).await.unwrap();

    let retrieved = get_user(user.id).await.unwrap();

    assert_eq!(retrieved.name, "Alice");
}
```

## Useful crates

- `tokio::test` — de-facto standard for async tests (`#[tokio::test(flavor = "multi_thread")]` when you need real parallelism)
- `rstest` — parameterized tests and fixtures (`#[rstest]`, `#[case(...)]`)
- `insta` — snapshot tests for stable serialized output
- `wiremock` — HTTP-level fakes; usually a better behavioral boundary than mocking your own HTTP client trait
