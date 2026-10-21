# Module Map: Testnet soak readiness

## Source

PRD: [#529: Make the engine ready for a 14-day unattended testnet soak](https://github.com/MarcosACH/tickwright/issues/529).
Decisions: [ADR-0061](../adr/0061-user-strategies-load-by-import-path.md), with amendments to
ADR-0024 (exit codes), ADR-0032 (strategies leave the `match`) and ADR-0041 (§3 `open_orders()`,
§7 the fixed constructor). Terms are used exactly as defined in `CONTEXT.md`.

Additions to the [v1 core-engine map](./v1-core-engine.md), which stands unchanged:

```
src/tickwright/
  domain/
    protocols.py              # Portfolio gains open_orders()
    order.py                  # + OpenOrderView  NEW
    errors.py                 # + BootRefusal marker, + the too-early read error  NEW
  engine/
    scoped_portfolio.py       # the scoped facade, moved out of portfolio.py  NEW
    strategy_host.py          # opens the read at the top of start()
    runner.py                 # portfolio_for builds the new facade, BootRefusal exits 2
  venues/hyperliquid/
    preflight.py              # the unreadable-mode case gets its own class
  app/
    config.py                 # StrategyConfig: strategy, strategy_id, symbols, settings
    build.py                  # resolve strategies first, construct after the engine
    __main__.py               # a strategy load refusal exits 2
  strategies/                 # each reference strategy gains a Settings model
Dockerfile                    # NEW
compose.example.yaml          # NEW
.github/workflows/            # + the image build and publish  NEW
```

## Modules

### Portfolio and OpenOrderView (`domain`, existing seam)

**Interface:** `Portfolio` gains a fourth method, `open_orders(symbol=None)`. It returns a tuple of
frozen `OpenOrderView`s for this strategy's orders that are not yet terminal. `PENDING` counts as
open. A view carries `signal_id`, `symbol`, `side`, `order_type`, `time_in_force`, `quantity`,
`filled_quantity`, `price`, `post_only`, `reduce_only`, `state` and `cancel_requested`. It carries
no `cloid` and no venue order id. The read raises a `domain` error until the strategies start. It
works from `restore()` onward. The other three methods are unchanged.

**Responsibilities:** Give a strategy the truth about its own working orders after a restart. The
startup barrier can settle an order before the strategy subscribes, so the strategy's snapshot alone
can be stale.

**Seams:** None new. `Portfolio` keeps one implementation by decision (ADR-0041 §8). It exists for
dependency direction.

**Depth note:** Without it, every strategy rebuilds its open orders from events it may never
receive. The orders already exist in the `Cache`. The seam only lets a `domain` strategy reach them.

---

### Scoped portfolio facade (`engine`, moved)

**Interface:** Built by `Engine.portfolio_for(strategy_id)` from the `PortfolioProjection`, the
`Cache` and the `StrategyHost`. The composition root still injects it into the constructor.

**Responsibilities:** Bind one `strategy_id` to both projections. Positions and the account come
from the projection. Open orders come from `Cache.open_orders(strategy_id=..., symbol=...)` and are
copied into `OpenOrderView`s. It asks the host whether the read is open and raises if not.

**Seams:** None.

**Depth note:** It moves out of `portfolio.py` because it now reads two projections. It holds no
state of its own. It stays a facade, so a strategy never holds the `Cache` or the projection.

---

### StrategyHost (`engine`, existing)

**Interface:** Unchanged for callers. It now owns one more fact: whether strategies may read open
orders.

**Responsibilities:** Open the read at the top of `start()`, before the first `restore()`. That is
the first moment the `Cache` holds venue-proven truth, because `start()` runs after the barrier.
The existing `_started` flag keeps its meaning for `stop()`. The new flag is separate.

**Seams:** None.

**Depth note:** The host is the one owner of "strategies have started". The runner opens nothing.

---

### Composition root (`app`, existing)

**Interface:** `build_engine(config)` as before. A bad strategy entry raises one refusal error before
any adapter is built.

**Responsibilities:** Two steps. **Resolve** runs at the very top of `_build_engine`, before the bus
and the store. It imports each `module:Class`, checks the class has `Settings`, and validates the
entry's settings with extra fields refused. **Construct** runs where the strategy `match` is today.
It passes exactly `emitter`, `portfolio` and `settings`. An exit run skips both steps, because it
builds no strategy.

**Seams:** None. Every other seam keeps its `match` (ADR-0032 as narrowed by ADR-0061).

**Depth note:** No separate loader module. The two steps are small, and both need what only the
root holds.

---

### StrategyConfig (`app`, existing)

**Interface:** Four fields: `strategy` (the import path as a string), `strategy_id`, `symbols` (a
list) and `settings`. `kind`, `symbol`, `side`, `quantity` and `price` go away. The reserved-id
refusal stays.

**Responsibilities:** Hold the entry. It never imports user code, so `AppConfig` stays pure (#71).

**Seams:** None.

**Depth note:** The strategy-specific fields move into each class's `Settings`, where the strategy
that reads them declares them.

---

### Engine and CLI exit codes (`engine`, `app`, `domain`, existing)

**Interface:** Exit `0` is a graceful stop. Exit `1` is a fault. Exit `2` is a boot refusal: a
config that does not validate, a strategy that does not load, a store bound to another account, or
an account mode the venue reported and the engine refuses.

**Responsibilities:** A `BootRefusal` marker class in `domain/errors.py`. `StoreAccountMismatch` and
the refused-mode case inherit it. The runner maps it to `2` in one place. `__main__` maps a config
`ValidationError` and the strategy load refusal to `2`. Two cases stay `1` so the supervisor
retries: a store another engine holds, and an account mode the engine could not read in time.

**Seams:** None.

**Depth note:** One marker means adding a refusal later is one base class, not a new branch in the
runner.

---

### Reference strategies (`strategies`, existing)

**Interface:** Each loads by import path, for example
`tickwright.strategies.single_shot:SingleShotMarketStrategy`. Each declares a `Settings` model and takes all
three constructor arguments, even one it ignores.

**Responsibilities:** Exercise the pipeline, now through the same path a user strategy takes.

**Seams:** They implement `Strategy`.

**Depth note:** Because they load like user strategies, every test and verify recipe that runs one
also tests the user path.

---

### Container image (packaging, new)

**Interface:** An image tagged `sha-<commit>` on every merge and `vX.Y.Z` on release, never
`latest`, for `amd64` and `arm64`. An example compose mounts the user's strategy code at run time,
puts it on the import path, and reads keys from files. It restarts on failure at most 3 times.

**Responsibilities:** Run the CLI in a container with no change to engine behavior. CI builds the
image and runs a hermetic paper replay inside it before publishing.

**Seams:** None.

**Depth note:** The README notes that a strategy with its own libraries needs a derived image. The
soak does not, because the exerciser needs no extra libraries.

## Docs that move with child 1

The `kind` field leaves the config, so these change in the same PR:

- `.env.example` (the `TICKWRIGHT_STRATEGIES` line)
- `README.md`, `docs/` and `CONTRIBUTING.md` (every `single_shot` example)
- the `/verify-tickwright` feature recipes that set a strategy

Child 2 also fixes `docs/extending.md`, which says a strategy cannot read its open orders.

## Dependency graph

```
app.build → app.StrategyConfig
app.build → user or reference strategy (resolved by import path)
app.build → engine.Engine.portfolio_for → engine.ScopedPortfolio
engine.ScopedPortfolio → engine.PortfolioProjection, engine.Cache, engine.StrategyHost
engine.ScopedPortfolio → domain.Portfolio, domain.OpenOrderView
engine.Runner → domain.BootRefusal
venues.hyperliquid.preflight → domain.BootRefusal
strategies → domain only
```

No cycles. `StrategyHost` does not import the facade. The facade asks the host one question.
Direction is `app → engine → domain`, as `import-linter` enforces (ADR-0032).

## Out of scope

- **A separate `Orders` seam.** It needs a fourth constructor argument (ADR-0041 §3 amendment).
- **Returning the mutable `Order`.** It carries `apply()`.
- **A separate strategy loader module.** Two small steps that need what only the root holds.
- **Importing user code inside `AppConfig`.** It would make the config read ambient code (#71).
- **The runner opening the read.** The host already owns strategy start.
- **Reusing `_started` as the gate.** It opens after every `restore()`, which is too late.
- **Exit `2` for a held store lock or an unreadable mode.** A retry can fix both.
- **A derived image in the example.** No build step for the soak. The README covers the case.
- **Any other seam loading by import path.** ADR-0061 covers strategies only.
