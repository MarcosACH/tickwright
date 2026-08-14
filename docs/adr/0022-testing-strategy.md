# Testing: real lightweight impls over mocks; hermetic default suite; opt-in live testnet suite

The testing thesis follows from the architecture: almost nothing needs *mocking* because every
internal seam has a **real lightweight implementation**. Tests wire `InMemoryBus` +
`SQLiteStore(:memory:)` + `PaperExchange` + `ManualClock` + a seeded RNG + `ReplayFeed` — all
real, all deterministic. **Mocking is confined to the one true process boundary: the Hyperliquid
HTTP/WS** (and the Kafka client when exercising that path). This makes "never mock our own
classes" structural rather than aspirational.

## Layers

- **Unit** — per component.
- **Property (hypothesis)** — the invariant catalog: duplicate-delivery convergence (ADR-0002),
  cross-symbol ordering never relied upon (ADR-0003), only-legal saga transitions
  (ADR-0007/0010), reconcile freezes on a failed read and never on `[]` (ADR-0011; the sentinel
  became a two-member `VenueReadFailure` in ADR-0049, and *how much* freezes turns on which
  member), retry-budget < ghost-grace timing (ADR-0008/0011),
  seq-never-reused-after-restart (ADR-0016), duplicate/stale
  ticks never reach `on_tick` (the per-symbol monotonic gate, ADR-0025).
- **Integration / E2E** — the tracer (`ReplayFeed → strategy → PaperExchange → OrderFilled` on
  `InMemoryBus` + `SQLiteStore`, fully deterministic, **zero external services**); a
  crash-recovery E2E (kill mid-saga, restart, assert snapshot-plus-reconcile converges); and
  named-event assertions (ADR-0020).

## Standing requirements

- Tests **never sleep** (`ManualClock`) and the default suite **never hits the network**.
- Duplicate deliveries are **actively injected** (ADR-0002).
- **≥90% coverage** on the core.

## Live tests

An **opt-in, separately-marked `live` suite** against Hyperliquid testnet,
**excluded from the PR/CI gate** (run manually/nightly). Its run-gate is a
dedicated `TICKWRIGHT_LIVE_TESTNET` opt-in flag that maps onto **no** config
field (issue #73) — distinct from the `TICKWRIGHT_HYPERLIQUID__SIGNING_KEY` the
suite reads for the key itself (ADR-0030). Gating on that config field instead
let a valid dummy key enrol the suite, which is why the hermeticity guard could
not run the whole suite; the separate flag removes that coupling. Keeps CI
hermetic and zero-setup while still giving real-venue coverage on demand. Live-in-gate was
rejected (flaky, needs secrets/network, breaks zero-setup CI).
