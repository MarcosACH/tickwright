# Toolchain: Python 3.13, async-first Hyperliquid transport, pydantic-settings config

- **Python 3.13** — the latest version `hyperliquid-python-sdk` 0.24 is classifier-tested against
  (it requires `>=3.9,<4.0`). The README's feared 3.10 pin is outdated as of SDK 0.24. (3.14 is
  outside the SDK's tested classifiers — not worth the risk for a reference repo.)
  **(Amended by #536, 3.13 is the floor and not the ceiling:** after #529 users install
  Tickwright into their own project. The `<3.14` cap refused every project on 3.14. So
  `requires-python` is now `>=3.13` with no upper bound. SDK 0.24 is still classifier-tested only
  up to 3.13. CI runs the whole suite on 3.14 too, and that suite drives the SDK paths we use.
  Development, the Docker image, ruff and mypy stay on 3.13, the lowest version we support.**)**
- **`hyperliquid-python-sdk ~=0.24`**, used **narrowly**. The SDK is **synchronous**
  (`websocket-client` + `requests`), and our engine is asyncio-throughout, so:
  - **Market-data feed** needs no auth → connect to `wss://api.hyperliquid.xyz/ws` directly with
    an **async** client (`websockets`/`aiohttp`). No SDK on the hot path.
  - **Order placement** → borrow the SDK / `eth-account` **signing** utilities only; the HTTP
    call is async (`aiohttp`).
  - **Rejected:** wrapping the whole sync SDK in `run_in_executor` — less code but parks blocking
    calls on a threadpool and muddies the determinism story. Async-first keeps the hot path
    non-blocking and the live adapter honestly async.
- **Config: `pydantic-settings` (v2)** — typed, immutable per-component config objects
  (`ExchangeConfig`, `StrategyConfig`, `BusConfig`, `StoreConfig`, …) loaded from environment +
  `.env`. Keys: `HYPERLIQUID_TESTNET`, `KAFKA_BOOTSTRAP_SERVERS`, `KAFKA_*_TOPIC`, `STORE_*`,
  `LOG_*`. Uses typed, immutable `*Config` objects.

- **Secrets:** the Hyperliquid signing key is **env-only** — never in committed files, never
  persisted to the store, and **redacted from structured logs** (ADR-0020). The default
  paper-exchange path needs no key at all.

Dependencies are managed with `uv` in a project venv, never installed globally.
