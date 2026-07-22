# Good and Bad Tests

## Good Tests

**Integration-style**: exercise real code paths through public interfaces. In this repo that
means wiring the real lightweight implementations — `InMemoryBus`, `SQLiteStore(":memory:")`,
`PaperExchange`, `ManualClock`, a seeded RNG, `ReplayFeed` — not mocks (ADR-0022). The default
suite runs with no external services and no API keys.

```python
# GOOD: observable behavior through the public interface
async def test_market_signal_fills_against_latest_tick(harness):
    # harness wires real bus + paper exchange + manual clock (module names illustrative —
    # take real names from CONTEXT.md and the module map)
    await harness.feed.emit(tick("BTC", price=Decimal("50000")))
    await harness.strategy.emit(signal("BTC", side=Side.BUY, qty=Decimal("0.1")))

    fill = await harness.next_event(OrderFilled)

    assert fill.price == Decimal("50000")
```

Characteristics:

- Tests behavior callers care about
- Uses public API only (no reaching into `_private` attributes or importing leaf internals)
- Survives internal refactors
- Describes WHAT, not HOW
- One logical assertion per test

Project specifics:

- **Tests never sleep.** Drive time with `ManualClock` (`clock.advance(...)`), never
  `time.sleep` or wall-clock waits — all time flows through the injected `Clock` Protocol
  (ADR-0005, ADR-0022).
- **Money is `Decimal`** (ADR-0029). A float literal in a price/quantity assertion is a bug in
  the test.
- **Duplicate deliveries are actively injected.** The bus is at-least-once; a behavior isn't
  green until it survives a replayed event (ADR-0002).
- Use `hypothesis` for invariant properties — ADR-0022 lists the catalog (duplicate-delivery
  convergence, legal-only saga transitions, reconcile freezes on `None`, …).
- Parametrize with `pytest.mark.parametrize` instead of copy-pasted near-duplicates.

## Bad Tests

**Implementation-detail tests**: coupled to internal structure.

```python
# BAD: asserts a method was called, not an outcome
async def test_strategy_calls_place_order(mocker):
    exchange = mocker.Mock(spec=PaperExchange)  # mocking our own class
    strategy = MomentumStrategy(exchange=exchange)

    await strategy.on_tick(tick("BTC", price=Decimal("50000")))

    exchange.place_order.assert_called_once()  # no observable outcome asserted
```

Red flags:

- `unittest.mock.Mock` / `patch` on our own classes or internal collaborators
- `assert_called_once_with(...)` / call-count/order assertions on internal methods
- Importing `_private` helpers or monkeypatching module internals so a test can poke inside
- Test breaks when refactoring without behavior change
- Test name describes HOW not WHAT
- Verifying through external means (raw SQL against the store, log lines, file bytes) instead
  of the interface

```python
# BAD: bypasses the interface to verify
async def test_checkpoint_writes_row(store):
    await saga.checkpoint(order)

    row = store._conn.execute(  # reaching into the store's connection
        "SELECT state FROM orders WHERE cloid = ?", (order.cloid,)
    ).fetchone()
    assert row[0] == "PENDING"


# GOOD: verifies through the interface
async def test_checkpoint_is_recoverable(store):
    await saga.checkpoint(order)

    recovered = await store.load_order(order.cloid)

    assert recovered.state is OrderState.PENDING
```

## Tautological Tests

The expected value restates the implementation, so the test passes by construction and can never disagree with the code — green the moment it's written, still green when the behavior breaks.

```python
# BAD: the expected value is recomputed the way the code computes it
def test_notional_is_price_times_qty(order):
    expected = order.price * order.qty          # the implementation IS price * qty
    assert notional(order) == expected          # passes by construction


# GOOD: expected value is an independent, known literal
def test_notional_is_price_times_qty():
    order = limit_order(price=Decimal("50000"), qty=Decimal("0.1"))
    assert notional(order) == Decimal("5000")   # worked by hand, from the spec
```

For a `hypothesis` property, the oracle must not be a copy of the function under test. Assert an **invariant** the code doesn't compute the same way — duplicate-delivery convergence, a saga never leaving a legal state, `reconcile` freezing on `None` (ADR-0022) — not the function's own output re-derived from the same inputs.

## Useful libraries

- `pytest` + `pytest-asyncio` — async tests against the asyncio runtime (ADR-0001)
- `hypothesis` — property-based tests; mandatory for the invariant catalog (ADR-0022)
- `pytest.mark.parametrize` — table-driven cases and fixtures
- `freezegun` is **not needed** here: time is injected via the `Clock` Protocol, so tests use
  `ManualClock` instead of patching the interpreter's clock (ADR-0005)
