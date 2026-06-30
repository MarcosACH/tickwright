# Interface Design for Testability

Good interfaces make testing natural:

1. **Accept dependencies via traits, don't construct them**

   ```rust
   // Testable — any PaymentGateway impl works (real, fake, mock)
   async fn process_order<G: PaymentGateway>(order: Order, gateway: &G) -> Result<Receipt> {
       gateway.charge(order.total_cents()).await
   }

   // Hard to test — concrete type, env coupling, no seam to swap
   async fn process_order(order: Order) -> Result<Receipt> {
       let gateway = StripeGateway::from_env();
       gateway.charge(order.total_cents()).await
   }
   ```

2. **Return values, don't mutate through `&mut` for results**

   ```rust
   // Testable — pure function, easy to assert on the return
   fn calculate_discount(cart: &Cart) -> Discount { /* ... */ }

   // Harder to test — must construct mutable state, then read it back
   fn apply_discount(cart: &mut Cart) {
       cart.total_cents -= /* ... */;
   }
   ```

3. **Use newtypes instead of primitive parameters**

   Rust's "value object". Prevents passing the wrong id type and gives you a place to hang validation.

   ```rust
   // Primitive obsession — easy to mix up, no validation seam
   fn get_user(id: String) -> Result<User> { /* ... */ }

   // Newtype — type system enforces the right id, validation lives here
   #[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
   pub struct UserId(Uuid);

   fn get_user(id: UserId) -> Result<User> { /* ... */ }
   ```

4. **Small surface area**
   - Fewer trait methods = fewer fakes/mocks to wire up
   - Fewer parameters = simpler test setup
   - Prefer one trait per role (`PaymentGateway`, `Mailer`) over a single god-trait
