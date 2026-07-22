# Tickwright

> Apache-2.0 · a readable, event-driven algorithmic trading engine (reference implementation).

Tickwright turns a market feed into orders through an event-driven pipeline —
`MarketFeed → Strategy → Exchange`, coordinated by an `EventBus` — with a crash-safe
order-lifecycle saga, idempotent recovery, and exchange reconciliation. It is a small,
fully-typed, heavily-tested Python core, built to be **read, tested, and extended** — not a
batteries-included trading product.

You bring a `MarketFeed`, a `Strategy`, and an `Exchange`. Tickwright gives you the event bus, the
order-lifecycle state machine, crash-safe recovery, and exchange reconciliation — the hard middle
that production trading systems get wrong.

## Why this exists

The open-source trading landscape is polarized: fast HFT engines you can't read, and retail bots
that are *products*, not architecture. There is no small, Python-native, **readable** engine that
demonstrates *correct* event-driven trading-system architecture — sagas, idempotent recovery, order
reconciliation — with a test suite you can trust and seams you can extend.

Tickwright fills that gap. Its value is **clarity and correctness, not speed or breadth.** You
should be able to read the whole core in an afternoon and understand exactly how a tick becomes an
order, and how the system recovers when the process dies mid-placement.

## v1 scope (deliberately narrow)

- **One real venue:** [Hyperliquid](https://hyperliquid.gitbook.io/hyperliquid-docs) — market data
  needs **no auth** (the `trades` WebSocket channel is unauthenticated), so the repo runs end-to-end
  with zero API keys. The write path (placing orders) needs a signing key; the paper default needs
  none.
- **One simulated venue:** a deterministic, in-process **paper exchange** — the default and the star
  of the MVP. It fills off the tick stream with a configurable fill model (slippage / partials /
  latency), so the repo is runnable and fully testable by anyone with zero setup.
- **Two event-bus backends:** `InMemoryBus` (instant, the default) and `KafkaBus` (the distributed
  story). Same `EventBus` interface.
- **Two durable stores:** `SQLiteStore` (file or in-memory, the default) and `PostgresStore`
  (production parity). Same `Store` interface.
- **Engine-only, live/paper execution.** Reference strategies (a one-shot market/limit order) exist
  only to exercise the pipeline. No strategy library, no UI.

**Two implementations per seam — no more.** One looks hardcoded; three is scope creep. Two proves
the abstraction is real.

## Non-goals (this is a feature, not a limitation)

- ❌ Not a universal "any exchange / any feed / build your platform" framework.
- ❌ Not competing on latency or throughput (it's Python, proudly so).
- ❌ **No backtesting.** v1 is live/paper execution only. The `ReplayFeed` is a *deterministic
  test/dev feed*, not a backtester — no portfolio simulation, no performance analytics.
- ❌ Not a strategy marketplace, indicator library, or research product.
- ❌ Not a plugin system with registries or config-DSLs. Extensibility is via **implementing a
  Protocol**, documented in [`docs/extending.md`](docs/extending.md) — nothing more.
- ❌ No GUI, notifications, or broker integrations beyond the two venues above.
- ❌ Not financial advice, and not certified for live money. A reference implementation.

## Quickstart

Requires **Python 3.13** and [uv](https://github.com/astral-sh/uv). Every command below works
verbatim on a fresh clone — the default stack (paper exchange + in-memory bus + SQLite + a
file-backed replay feed) needs **no external services and no API keys**.

```bash
# 1. Create the project venv and install the locked dependencies.
uv venv
uv sync

# 2. Run the test suite (unit + property tests; the hermetic default path).
uv run pytest

# 3. Run the paper engine on the bundled sample tick stream.
cp .env.example .env
uv run tickwright
```

Step 3 replays [`examples/ticks.jsonl`](examples/ticks.jsonl) through the demo `single_shot_market`
strategy into the paper exchange, and you'll see the order lifecycle on stdout as structured JSON:

```json
{"event": "engine.barrier_cleared", "run_id": "run-…", "level": "info", …}
{"event": "engine.feed_started", "run_id": "run-…", "level": "info", …}
{"event": "order.placed", "signal_id": "demo:BTC:1", "level": "info", …}
{"event": "order.submitted", "signal_id": "demo:BTC:1", "level": "info", …}
{"event": "order.filled", "cloid": "0x…", "level": "info", …}
```

The engine keeps running after the replay drains (a live/paper engine waits for more work and its
reconciliation cadence — it does not self-exit on end-of-file). Press **Ctrl-C** to stop it: it
shuts down gracefully, takes final strategy snapshots, leaves resting orders alone, and exits `0`.

Everything is configured through the environment / `.env` — [`.env.example`](.env.example) is the
canonical variable reference, and every variable maps onto a field of `AppConfig`
([`src/tickwright/app/config.py`](src/tickwright/app/config.py)) with the `TICKWRIGHT_` prefix. To
switch the fill model to `stochastic`, swap in the Kafka bus or Postgres store, or point the live
Hyperliquid feed at real market data, edit `.env` — no code changes.

## Architecture at a glance

```
   ticks              signals             orders / lifecycle
MarketFeed ─────▶ Strategy ─────▶ Exchange ─────▶ (venue)
    │                 │                │
    └────────┬────────┴────────┬───────┘
             ▼                  ▼
        ┌──────────────────────────────────┐
        │             EventBus              │   InMemoryBus | KafkaBus
        └──────────────────────────────────┘
                        │
        ┌───────────────┴──────────────────┐
        │  Engine core (the hard middle):   │
        │  • order-lifecycle saga           │   ExecutionManager
        │  • reconciliation loop            │   Reconciliation (+ ghost gate)
        │  • idempotent recovery            │   Cache (write-through read-model)
        │  • pre-trade guard + kill switch  │   PreTradeGuard
        │  • durable checkpoints            │   Store   SQLite | Postgres
        └───────────────────────────────────┘
```

The `EventBus` is the only coupling between components: everything publishes and subscribes by event
type. The **order lifecycle** is a saga/state machine (`PENDING → SUBMITTED → LIVE → FILLED /
CANCELLED / …`) whose transitions checkpoint to the `Store` **before** any network send, so a crash
mid-placement is recoverable. **Reconciliation** periodically compares local saga state against
venue truth, with a grace period and a `None`-vs-`[]` connectivity-failure guard so an outage is
never misread as "all orders vanished." **Recovery** replays from the store and rebuilds the `Cache`
read-model; restarting converges to the same state — no double-fills, no orphaned orders.

The runtime is a **single `asyncio` process**. All time flows through an injected `Clock` (so the
test suite never sleeps), and every state-affecting path emits a named observability event.

### Package layout

```
src/tickwright/
  domain/         # events, seam Protocols, value types, the order FSM — depends on nothing
  engine/         # ExecutionManager, Reconciliation, Cache, PreTradeGuard, StrategyHost, runner
  adapters/       # bus/ (InMemory|Kafka), store/ (SQLite|Postgres), feed/ (Replay),
                  #   paper/ (PaperExchange + FillModel), clock/ (Live|Manual)
  venues/         # hyperliquid/ — the one self-contained live venue package
  strategies/     # minimal reference Strategy impls
  observability/  # named-event catalog, correlation ids, structured logging
  app/            # the composition root: build_engine(config) + the CLI entry
```

Dependencies point one way: `app` knows every concrete; `engine` depends only on `domain` Protocols;
adapters never import each other. The direction is enforced in CI by `import-linter`
([ADR-0032](docs/adr/0032-package-topology-dependency-direction-composition-root.md)).

## Learn more

- **[`CONTEXT.md`](CONTEXT.md)** — the domain glossary; every term (Engine, Event, EventBus, saga,
  reconciliation, ghost, …) resolved. Start here.
- **[`docs/module-maps/v1-core-engine.md`](docs/module-maps/v1-core-engine.md)** — the architecture
  anchor: each module's interface, responsibilities, seams, and why it earns its place.
- **[`docs/extending.md`](docs/extending.md)** — the pull-then-subscribe strategy contract, and
  checklists for adding a strategy, a venue, or a bus/store backend.
- **[`docs/adr/`](docs/adr/)** — one Architecture Decision Record per load-bearing decision, with the
  alternatives rejected and why.
- **[`CONTRIBUTING.md`](CONTRIBUTING.md)** — setup, checks, git hooks, and how we work (TDD, vertical
  slices, docs-sync).

## Development

```bash
uv run pytest -v          # tests (property tests via hypothesis; ≥90% coverage on the core)
uv run ruff check .       # lint
uv run ruff format .      # format
uv run mypy .             # type-check
uv run lint-imports       # dependency-direction boundaries (ADR-0032)
```

The non-default backends are opt-in and need infrastructure: `docker compose up -d postgres` for the
`PostgresStore` path and `docker compose up -d kafka` to run the app on the `KafkaBus`. The
`postgres`- and `live`-marked test tiers auto-skip when their service (a reachable Postgres, a funded
Hyperliquid **testnet** key) isn't configured, so a bare `uv run pytest` stays green; the `KafkaBus`
adapter runs against an in-process fake broker and needs no infrastructure. See the test-tier
breakdown in [`CONTRIBUTING.md`](CONTRIBUTING.md) and the variables in [`.env.example`](.env.example).

## License

[Apache-2.0](LICENSE).
