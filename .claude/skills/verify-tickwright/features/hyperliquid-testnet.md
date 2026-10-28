# Hyperliquid testnet

Real orders on the testnet exchange with the operator's funded testnet key. One round trip
proves the signing path, the venue fill report, the venue-reported fee, the ledger that opens
from the venue's balance, the 60 second account reconcile cadence, and that the key never reaches
a log. A second life closes half a position first, so a reconcile runs while the rest is still
open. Testnet only. Mainnet placement is out of scope for this skill.

## Sub-features

- `tn-materialise` the ledger opens at the venue's cash (`account.materialised`).
- `tn-round-trip` buy 0.001 BTC at market, hold three ticks, sell. Flat at the end, on the venue
  too.
- `tn-fee` `positions.fees` equals the sum of every venue-reported fee, one per partial fill.
- `tn-reconcile` `account.reconciled` fires on the cadence.
- `tn-cash` the ledger cash never needs a heal on a fresh account that only this run traded.
  `account.healed` must not fire.
- `tn-redaction` the signing key appears nowhere in the evidence.
- `tn-divergence` a clean round trip raises no `valuation.divergence`.
- `tn-partial` buy 0.002 BTC, sell 0.001, then sell the rest. At least one reconcile runs while
  half the position is open, and the cash still needs no heal (#582).

## How to get to it (user POV)

- A `.env` with `TICKWRIGHT_FEED=hyperliquid`, `TICKWRIGHT_EXCHANGE=hyperliquid`,
  `TICKWRIGHT_HYPERLIQUID__TESTNET=true`, the key, and a strategy. `uv run tickwright`.

## Driving it with verify

Preconditions:

- `$V doctor` says the repo `.env` has a signing key. The key is a funded testnet API wallet.
- The testnet account holds no BTC position, or holds one whose leverage you are not changing.
- Run id `testnet` is unused. Budget about thirteen minutes.

- **Run.** Run `$V init testnet --feature hyperliquid-testnet` and
  `$V start testnet rt --preset testnet --forward-key --strategy round_trip_then_flat --param QUANTITY=0.001 --param HOLD_TICKS=3 --param WAIT_SECONDS=10`.
- **Materialise.** Run `$V await testnet rt --event account.materialised --timeout 60`. The line
  carries the qualified `account_id` (`hyperliquid-testnet-0x...`) and the venue's cash as
  `genesis_collateral`.
- **Round trip.** Run `$V await testnet rt --event verify.flat --timeout 240`.
- **Reconcile.** Run `$V await testnet rt --event account.reconciled --timeout 90`.
- **Stop.** Run `$V signal testnet rt TERM` at once, then `$V await testnet rt --exit`,
  `$V dump testnet rt`.
- **Venue fees.** Run `FEES=$($V venue-fills testnet rt)`. It writes `rt.venue-fills.json` and
  prints the fee sum over every fill whose oid is a `venue_oid` in the `orders` rows. Not the
  cloid: every run places the same cloids, so a cloid cut would adopt the last run's fills.
- **Check the round trip.** Run these before the `pc` life starts, because `check --sql` reads
  the store named in the run's current `.env`, and the `pc` life replaces it.
  Run `$V check testnet rt tn-materialise --event account.materialised --expect 1`,
  `$V check testnet rt tn-round-trip --sql "select count(*) from orders where state='filled'" --expect 2`,
  `$V check testnet rt tn-round-trip --sql "select signed_size from positions" --expect 0.000`,
  `$V check testnet rt tn-round-trip --exit --expect 0`,
  `$V check testnet rt tn-fee --sql "select fees from positions" --expect $FEES`,
  `$V check testnet rt tn-reconcile --event account.reconciled --expect 1`,
  `$V check testnet rt tn-cash --event account.healed --expect 0`,
  `$V check testnet rt tn-round-trip --event verify.flat --expect 1` (the run reached the end),
  `$V check testnet rt tn-divergence --event valuation.divergence --expect 0`,
  `$V check testnet rt tn-round-trip --sql "select isolated_collateral from positions" --expect 0`
  (a closed position holds a known 0, not NULL, #524).
- **Partial close.** Run
  `$V start testnet pc --preset testnet --forward-key --env TICKWRIGHT_SQLITE__PATH=pc.db --env 'TICKWRIGHT_STRATEGIES=[{"strategy": "partial_close:PartialClose", "strategy_id": "partial", "symbols": ["BTC"], "settings": {"quantity": "0.001", "wait_seconds": 10, "hold_seconds": 75}}]'`.
  Its own store, so its ledger opens fresh at the venue's cash. Then run
  `$V await testnet pc --event verify.flat --timeout 300`, `$V signal testnet pc TERM`,
  `$V await testnet pc --exit`, `$V dump testnet pc`, and `PC_FEES=$($V venue-fills testnet pc)`.
- **Half-open reconcile.** Run
  `jq -r 'select(.event == "verify.fill" or .event == "verify.flat" or .event == "account.reconciled") | .phase // "reconciled"' .agents/verify/testnet/evidence/pc.stderr.jsonl | awk '$0 == "held_half" {h = 1; next} $0 == "flat" {h = 0} h && $0 == "reconciled" {n++} END {print (n ? "yes" : "no")}' > .agents/verify/testnet/evidence/pc.half-reconciled.txt`.
  It writes `yes` when a reconcile ran between the partial close and the final close.
  With a suffixed run id, change both paths.
- **Check the partial close.** Run
  `$V check testnet pc tn-partial --sql "select count(*) from orders where state='filled'" --expect 3`,
  `$V check testnet pc tn-partial --sql "select signed_size from positions" --expect 0.000`,
  `$V check testnet pc tn-partial --exit --expect 0`,
  `$V check testnet pc tn-partial --event verify.flat --expect 1`,
  `$V check testnet pc tn-partial --file pc.half-reconciled.txt --expect yes`,
  `$V check testnet pc tn-fee --sql "select fees from positions" --expect $PC_FEES`,
  `$V check testnet pc tn-cash --event account.healed --expect 0`,
  `$V check testnet pc tn-divergence --event valuation.divergence --expect 0`.
- **Key scan.** Run `$V secrets-check testnet > .agents/verify/testnet/evidence/secrets.txt`.
  It runs after both `venue-fills` so the scan covers every evidence file. The file holds
  `no signing key in evidence` or nothing. A hit goes to stderr as file paths, never the key.
  With a suffixed run id, change the path too. Then run
  `$V check testnet rt tn-redaction --file secrets.txt --expect "no signing key in evidence"`.
- **Report.** Run `$V report testnet`. Expected verdict: `PASS`. A FAIL that matches a Gotcha
  below is known. Any other FAIL is new, and `$V issue-draft testnet` drafts it.
- **Cleanup.** Run `$V cleanup testnet`.

## Gotchas

- A buy sent before the first mark is never sent. It ends `failed` and the engine keeps running
  ([#509](https://github.com/MarcosACH/tickwright/issues/509)). The round trip needs the buy to
  fill, so the `rt` life waits 10 seconds with `--param WAIT_SECONDS=10`.
- On live, `margin_used` and `liquidation_price` read `null` in the `verify.fill` and
  `verify.tick` lines until a reconcile posts the venue's numbers (#517, #519). That is the
  "unknown, not zero" rule, not a failure. Mid-hold, `positions.isolated_collateral` can be NULL
  for the same reason.

- Every run places the same cloids on the venue, because each cloid is derived from
  its signal id, `round_trip:BTC:1` for the buy and `round_trip:BTC:2` for the sell. The engine
  reads and cancels by oid once it has one, and before the ack it refuses a record placed before
  the saga was created (#354). A run should not adopt an older
  run's fill. If one does, it is a new bug.
- The key is forwarded from the repo `.env` into the child environment only. Never write it with
  `--env`. The helper refuses.
- `feed.lagged` lines are normal on testnet. The stream conflates.
- The venue fills one 0.001 BTC market order in several partial fills, each with its own fee.
  `positions.fees` sums them all (`domain/position.py`). `venue-fills` does the same on the
  venue side. A count of fill rows is never a check.
- `tn-reconcile` expects exactly one `account.reconciled`. The cadence is 60 seconds and keeps
  firing until the process stops, so send `TERM` right after the await. Anything you do in
  between, such as a venue query, can let a second one land.
- **Not in the map yet: the leverage push.**
  `--param 'LEVERAGE={"BTC": {"mode": "cross", "leverage": 5}}'` pushes at boot. If the account
  holds a BTC position at a different leverage, the boot refuses to start by design. Close the
  position first. When you add it as a sub-feature, the check is
  `--event exchange.leverage_unchanged --expect 0`. The event only fires for a symbol the account
  already holds at the same leverage (`venues/hyperliquid/preflight.py`). On a flat account the
  push writes silently, so the check needs a held position to mean anything.
- **`tn-partial` needs each hold to outlast the 60 second cadence.** `hold_seconds` is wall
  time from the fill, and the next step waits for a tick after it. At 75 seconds, every hold
  holds at least one reconcile. Below 60, `pc.half-reconciled.txt` can read `no` on a correct
  engine. The partial close often fills in several pieces. Hyperliquid rounds the cost basis on
  each one, which is the path #582 fixed.
- `HOLD_TICKS` counts real testnet trades. On a quiet market three ticks can take a minute.
- A `verify.flat` read of `account.equity` can be `null` when no mark has arrived since the
  fill. That is the documented "unknown, not zero" rule, not a failure.
- **`tn-divergence` only tests the isolated margin when a reconcile lands mid-hold (#402).**
  Before #402, a reconcile while the position was open raised a false `valuation.divergence` on
  `margin_used` and `free_margin`. A round trip that closes within 60 seconds of
  `account.materialised` never runs that path, so its PASS says nothing about it. To test it,
  raise `HOLD_TICKS` until the hold outlasts 60 seconds. Then confirm that `account.reconciled`
  comes before `position.closed` in `rt.stderr.jsonl`. If the close comes more than 120 seconds
  after `account.materialised`, a second reconcile lands, so `tn-reconcile` then fails by design.
