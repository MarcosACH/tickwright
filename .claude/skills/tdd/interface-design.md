# Interface Design for Testability

Good interfaces make testing natural:

1. **Accept dependencies via Protocols, don't construct them**

   ```python
   # Testable — any Clock impl works (LiveClock in prod, ManualClock in tests)
   class Reconciler:
       def __init__(self, *, transport: ExchangeTransport, clock: Clock) -> None: ...


   # Hard to test — concrete type, env coupling, no seam to swap
   class Reconciler:
       def __init__(self) -> None:
           self._transport = HyperliquidTransport.from_env()
   ```

   This is how ADR-0005 works: engine code never touches `time.time()` or `asyncio.sleep`
   directly — the injected `Clock` owns all time, so tests advance a `ManualClock` instead of
   sleeping.

2. **Return values, don't mutate through parameters for results**

   ```python
   # Testable — pure function, easy to assert on the return
   def size_order(signal: Signal, book: OrderBook) -> Quantity: ...


   # Harder to test — must construct mutable state, then read it back
   def apply_sizing(signal: Signal, book: OrderBook, order: MutableOrder) -> None:
       order.qty = ...
   ```

3. **Use owned types instead of primitive parameters**

   `NewType` for identities, frozen dataclasses for values — invalid states become
   unrepresentable and validation gets one home:

   ```python
   # Primitive obsession — any str passes, cloids and symbols interchangeable
   def get_order(cloid: str) -> Order: ...


   # Owned types — mixups are type errors, caught by mypy
   Cloid = NewType("Cloid", str)
   Symbol = NewType("Symbol", str)


   def get_order(cloid: Cloid) -> Order: ...
   ```

   Events are frozen dataclasses (ADR-0025); money is `Decimal`, never `float` (ADR-0029).
   `Literal` / `Enum` for closed sets like order side — not bare `str`.

4. **Small surface area**

   - Fewer Protocol methods = fewer fake methods to write
   - Keyword-only parameters (`def __init__(self, *, ...)`) = self-documenting test setup
   - Prefer one Protocol per role (`Clock`, `ExchangeTransport`, `OrderStore`) over a single
     god-interface
