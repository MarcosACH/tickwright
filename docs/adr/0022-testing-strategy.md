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
**excluded from the PR/CI gate** (run on a schedule and on demand). Its run-gate is a
dedicated `TICKWRIGHT_LIVE_TESTNET` opt-in flag that maps onto **no** config
field (issue #73) — distinct from the `TICKWRIGHT_HYPERLIQUID__SIGNING_KEY` the
suite reads for the key itself (ADR-0030). Gating on that config field instead
let a valid dummy key enrol the suite, which is why the hermeticity guard could
not run the whole suite; the separate flag removes that coupling. Keeps CI
hermetic and zero-setup while still giving real-venue coverage on demand. Live-in-gate was
rejected (flaky, needs secrets/network, breaks zero-setup CI).

**Where it runs (issue #255).** `.github/workflows/ci-live.yml` carries it:
`workflow_dispatch` plus a **weekly** cron (Mondays 10:17 UTC), never a required
status check. This ADR long said "manually/nightly" while nothing scheduled it
at all — the workflow closes that gap, and settles on weekly rather than nightly
deliberately. The tier's unique value is detecting **venue drift**, a
calendar-time risk our faked boundary cannot see; that does not accrue daily,
and while the project is pre-1.0 and trading no real money a 7-day window is
ample. The binding cost is attention, not runner minutes: a 3am red on a week
with no commits teaches its only reader to ignore it. **Escalate to nightly when
real money is in play** — that trigger, not a change in commit rate.

**What it does not cover.** The suite constructs `HyperliquidExchange` and calls
`place()` directly, never `start()`, so ADR-0046's account abstraction-mode boot
gate — invoked only from `start()` — is not exercised live. Nor can it be from
this account: it is in `unifiedAccount` mode, which that gate refuses by design,
and switching it is a user-signed action an agent wallet cannot perform.
