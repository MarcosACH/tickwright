# Python Review Pitfalls

Annotated bad-vs-good examples for the highest-impact findings. Each entry maps to a checklist item in [SKILL.md](SKILL.md). The reviewer cites these to write concrete, actionable comments.

---

## 1. Bare `except:` or `except Exception:`

**Category**: errors • **Severity**: BLOCKING in request/IO paths, WARN elsewhere

```python
# BAD — swallows KeyboardInterrupt, SystemExit, programming bugs
try:
    order = await place_order(signal)
except:
    return None
```

```python
# GOOD — narrow exception type, preserve chain, structured failure
try:
    order = await place_order(signal)
except OrderRejected as e:
    logger.warning("order_rejected", signal_id=signal.id)
    raise ExecutionError("order rejected") from e
```

**Comment template**:
```
### [BLOCKING] R### — exchange/adapter.py:42 — bare except in order path
**Category**: errors
**Why**: swallows KeyboardInterrupt and masks real bugs; rejected orders become silent None returns.
**Fix**: catch `OrderRejected` specifically and `raise ExecutionError(...) from e`.
**Status**: open
```

The `except Exception:` form is acceptable ONLY at process boundaries (top-level worker loop, signal handlers) where the alternative is the process dying. Even then, log the exception with `exc_info=True`.

---

## 2. Mutable default arguments

**Category**: errors • **Severity**: BLOCKING (always — this is a classic Python footgun)

```python
# BAD — the list is shared across ALL calls
def append_order(order, history=[]):
    history.append(order)
    return history
```

```python
# GOOD — None sentinel
def append_order(order, history: list | None = None):
    if history is None:
        history = []
    history.append(order)
    return history
```

Same trap with `dict`, `set`, `dataclass` fields without `field(default_factory=...)`.

---

## 3. Sync I/O inside `async def`

**Category**: async • **Severity**: BLOCKING

```python
# BAD — blocks the entire event loop
async def load_config(path: Path) -> Config:
    with open(path) as f:  # sync I/O
        return json.load(f)


async def fetch_price():
    return requests.get(URL).json()  # sync HTTP — blocks every coroutine
```

```python
# GOOD — async I/O
async def load_config(path: Path) -> Config:
    async with aiofiles.open(path) as f:
        data = await f.read()
    return json.loads(data)


async def fetch_price():
    async with aiohttp.ClientSession() as s:
        async with s.get(URL) as r:
            return await r.json()
```

Same trap with `time.sleep` (use `await asyncio.sleep`) and `threading.Lock` held across an `await` (use `asyncio.Lock`, or release before awaiting).

For CPU-bound work inside async, hand off via `asyncio.to_thread(...)` or `loop.run_in_executor(...)`.

---

## 4. Locks / sessions held across `await`

**Category**: async • **Severity**: WARN (BLOCKING when it can deadlock)

```python
# BAD — lock held while awaiting; another task waiting on this lock blocks
async with lock:
    result = await slow_rpc_call()
    update_state(result)
```

```python
# GOOD — minimize the critical section
result = await slow_rpc_call()
async with lock:
    update_state(result)
```

Shared async resources (a connection, a session) are **not** safe to share across `asyncio.create_task`. Each task gets its own:

```python
# BAD — both tasks share the connection; second op may see partial state
async def runner(conn):
    await asyncio.gather(handler_a(conn), handler_b(conn))


# GOOD — one connection per task
async def runner(conn_factory):
    async def with_conn(coro):
        async with conn_factory() as c:
            await coro(c)

    await asyncio.gather(with_conn(handler_a), with_conn(handler_b))
```

---

## 5. `assert` for runtime validation

**Category**: errors • **Severity**: BLOCKING

```python
# BAD — assert disappears under `python -O`; silent failure in production
def submit(amount: Decimal):
    assert amount > 0, "amount must be positive"
    ...
```

```python
# GOOD — explicit raise
def submit(amount: Decimal):
    if amount <= 0:
        raise ValueError(f"amount must be positive, got {amount}")
    ...
```

`assert` is fine in tests; never in production validation paths.

---

## 6. `raise NewError(...)` without `from`

**Category**: errors • **Severity**: WARN

```python
# BAD — loses original traceback
try:
    payload = json.loads(body)
except json.JSONDecodeError:
    raise BadRequest("invalid json")
```

```python
# GOOD — preserve the chain
try:
    payload = json.loads(body)
except json.JSONDecodeError as e:
    raise BadRequest("invalid json") from e
```

Use `from None` only when you deliberately want to suppress the original (rare — usually a sign the wrong exception type is being caught).

---

## 7. Public API leaks of third-party types

**Category**: api • **Severity**: WARN

```python
# BAD — caller now depends on the exchange SDK's response shape; SDK swap = breaking change
async def get_order(order_id: str) -> ExchangeSdkOrder:
    return await sdk.fetch_order(order_id)
```

```python
# GOOD — domain type owned by your module
@dataclass(frozen=True)
class OrderView:
    id: str
    symbol: str
    status: OrderStatus


async def get_order(order_id: str) -> OrderView:
    raw = await sdk.fetch_order(order_id)
    return OrderView(id=raw.id, symbol=raw.symbol, status=_map_status(raw.status))
```

Exception: a service that exists explicitly to wrap a library may expose its types (e.g., a thin `ExchangeClient` wrapper can expose the SDK's session type since wrapping is its purpose).

---

## 8. String concatenation in hot loops

**Category**: performance • **Severity**: WARN

```python
# BAD — O(n²) due to immutable strings
report = ""
for line in lines:
    report += f"{line}\n"
```

```python
# GOOD — join is O(n)
report = "\n".join(lines) + "\n"
# or, with formatting per line:
report = "".join(f"{line}\n" for line in lines)
```

For incremental building inside a loop body (e.g., conditional pieces), use `io.StringIO` and `.write()`.

---

## 9. Mocking internal collaborators

**Category**: testing • **Severity**: WARN

```python
# BAD — mocks the repository of our own service; test passes when repo is broken
mock_store = Mock(spec=OrderStore)
mock_store.get_open.return_value = [stub_order]
svc = OrderService(store=mock_store)
assert svc.list_open() == [stub_order]
```

```python
# GOOD — real store, in-memory backend; the boundary is the storage engine
store = OrderStore(backend=in_memory_backend)
store.add(stub_order)
svc = OrderService(store=store)
assert svc.list_open() == [stub_order]
```

Mock only at process boundaries: HTTP (the exchange SDK), the event-bus transport, the system clock, randomness, filesystem. Mocking your own modules is how you ship green tests over broken behavior.

For the exchange SDK in Tickwright: mock at the SDK client class level, not at the `ExchangeAdapter` (which is *our* abstraction over the SDK). Prefer the in-process deterministic paper exchange over mocks where it exercises the same seam.

---

## 10. `from x import *`

**Category**: api • **Severity**: WARN

```python
# BAD — every internal symbol leaks; renames silently break callers
from tickwright.strategy.engine import *
```

```python
# GOOD — explicit, reviewable surface
from tickwright.strategy.engine import (
    Strategy,
    StrategyContext,
)
```

The cost of typing the names is the cost of a SemVer review, which is the point. In each package, declare `__all__` explicitly so star imports (from elsewhere) get a curated surface.

---

## 11. Mutable global state across workers

**Category**: concurrency • **Severity**: BLOCKING when crossing workers, WARN within one event loop

```python
# BAD — running_strategies mutated by both the bus handler and main loop without lock
running_strategies: dict[str, Strategy] = {}


async def handle_start(strategy_id: str):
    running_strategies[strategy_id] = Strategy(...)


async def handle_bus_update(msg):
    running_strategies[msg["strategy_id"]].config.threshold = msg["threshold"]
```

```python
# GOOD — explicit asyncio.Lock around mutations, or a single owner task that processes a queue
running_strategies: dict[str, Strategy] = {}
_strategies_lock = asyncio.Lock()


async def handle_start(strategy_id: str):
    strat = Strategy(...)
    async with _strategies_lock:
        running_strategies[strategy_id] = strat
```

This is especially load-bearing where an event-bus handler races with the main consumer loop over shared in-memory state.

---

## 12. Floats in financial calculations

**Category**: performance / errors • **Severity**: BLOCKING

```python
# BAD — float drift; 0.1 + 0.2 != 0.3
total_cost = price * quantity
if total_cost < 10.5:
    bump_qty()
```

```python
# GOOD — Decimal end-to-end, never compare floats for equality in money paths
total_cost = Decimal(str(price)) * Decimal(str(quantity))
if total_cost < MIN_NOTIONAL:
    bump_qty()
```

Tickwright invariant: order quantities and prices are carried as `Decimal` end-to-end, and notional comparisons are done in `Decimal`. A regression to `float` here causes rounding drift that the exchange (or the deterministic paper exchange) rejects.

---

## 13. `pass`-only `except` blocks

**Category**: errors • **Severity**: BLOCKING

```python
# BAD — silently drops failures; you'll never know why orders didn't post
try:
    await exchange.place(...)
except Exception:
    pass
```

```python
# GOOD — at minimum, log; usually re-raise or convert to a typed event
try:
    await exchange.place(...)
except Exception as e:
    logger.exception("order_place_failed", order_id=order_id)
    return make_rejected_event(signal, str(e))
```

Specifically in Tickwright, a rejected order MUST propagate as an explicit rejection event. A `pass`-only except in the exchange-adapter path violates the order-rejection-propagation contract and leaves the order-lifecycle saga stuck.
