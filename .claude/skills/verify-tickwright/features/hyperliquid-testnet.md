# Hyperliquid testnet

Real orders on the testnet exchange with the operator's funded testnet key. One round trip
proves the signing path, the venue fill report, the venue-reported fee, the ledger that opens
from the venue's balance, the 60 second account reconcile cadence, and that the key never reaches
a log. Testnet only. Mainnet placement is out of scope for this skill.

## Sub-features

- `tn-materialise` the ledger opens at the venue's cash (`account.materialised`).
- `tn-round-trip` buy 0.001 BTC at market, hold three ticks, sell. Flat at the end, on the venue
  too.
- `tn-fee` `positions.fees` equals the sum of the two venue-reported fees.
- `tn-reconcile` `account.reconciled` fires on the cadence.
- `tn-cash` the ledger cash never needs a heal on a fresh account that only this run traded.
  `account.healed` must not fire.
- `tn-redaction` the signing key appears nowhere in the evidence.

## How to get to it (user POV)

- A `.env` with `TICKWRIGHT_FEED=hyperliquid`, `TICKWRIGHT_EXCHANGE=hyperliquid`,
  `TICKWRIGHT_HYPERLIQUID__TESTNET=true`, the key, and a strategy. `uv run tickwright`.

## Driving it with verify

Preconditions:

- `$V doctor` says the repo `.env` has a signing key. The key is a funded testnet API wallet.
- The testnet account holds no BTC position, or holds one whose leverage you are not changing.
- Run id `testnet` is unused. Budget about eight minutes.

- **Run.** Run `$V init testnet --feature hyperliquid-testnet` and
  `$V start testnet rt --preset testnet --forward-key --strategy round_trip_then_flat --param QUANTITY=0.001 --param HOLD_TICKS=3`.
- **Materialise.** Run `$V await testnet rt --event account.materialised --timeout 60`. The line
  carries the qualified `account_id` (`hyperliquid-testnet-0x...`) and the venue's cash as
  `genesis_collateral`.
- **Round trip.** Run `$V await testnet rt --event verify.flat --timeout 240`. Then read the
  two venue fills:
  `curl -s -X POST https://api.hyperliquid-testnet.xyz/info -H 'Content-Type: application/json' -d '{"type":"userFills","user":"<account address>"}' | jq '.[0:2][] | {px, sz, closedPnl, fee, dir, cloid}' > .agents/verify/testnet/evidence/rt.venue-fills.json`.
  The two cloids match the `orders` rows.
- **Reconcile.** Run `$V await testnet rt --event account.reconciled --timeout 90`.
- **Stop.** Run `$V signal testnet rt TERM`, `$V await testnet rt --exit`, `$V dump testnet rt`,
  `$V secrets-check testnet`.
- **Check.** Run `$V check testnet rt tn-materialise --event account.materialised --expect 1`,
  `$V check testnet rt tn-round-trip --sql "select count(*) from orders where state='filled'" --expect 2`,
  `$V check testnet rt tn-round-trip --sql "select signed_size from positions" --expect 0.000`,
  `$V check testnet rt tn-round-trip --exit --expect 0`,
  `$V check testnet rt tn-fee --sql "select fees from positions" --expect <sum of the two venue fees>`,
  `$V check testnet rt tn-reconcile --event account.reconciled --expect 1`,
  `$V check testnet rt tn-cash --event account.healed --expect 0`,
  `$V check testnet rt tn-redaction --event verify.flat --expect 1` (the run reached the end)
  and read the `secrets-check` line: `no signing key in evidence`.
- **Report.** Run `$V report testnet`. Expected verdict today: `FAIL (1 of 8 checks failed)`,
  on `tn-cash`. See Gotchas. `$V issue-draft testnet` drafts it.
- **Cleanup.** Run `$V cleanup testnet`.

## Gotchas

- **Known FAIL (2026-09-16), `tn-cash`.** Right after the opening fill the strategy read
  `account.cash 942.920480` while the ledger had opened at `929.16731` and the fill carried a
  `0.034336` fee and no realized PnL. Nothing in the trail explains the +13.79. The 60 second
  reconcile then reported `tier_1: 1` and healed cash to the venue's `929.076648`, which is the
  right number. The heal masks it in the final dump, so the check is on the `account.healed`
  event, and the first `verify.fill` line holds the wrong number. A second run the same day
  reproduced it: genesis `929.076648`, cash after the opening fill `942.829986`. The jump is
  `+13.753` both times, at two different fill prices, so it is not a function of the trade.
- The key is forwarded from the repo `.env` into the child environment only. Never write it with
  `--env`. The helper refuses.
- `feed.lagged` lines are normal on testnet. The stream conflates.
- **Not in the map yet: the leverage push.**
  `--param 'LEVERAGE={"BTC": {"mode": "cross", "leverage": 5}}'` pushes at boot. If the account
  holds a BTC position at a different leverage, the boot refuses to start by design. Close the
  position first. When you add it as a sub-feature, the check is
  `--event exchange.leverage_unchanged --expect 0`: the catalog says a push that landed is the
  absence of that event.
- `HOLD_TICKS` counts real testnet trades. On a quiet market three ticks can take a minute.
- A `verify.flat` read of `account.equity` can be `null` when no mark has arrived since the
  fill. That is the documented "unknown, not zero" rule, not a failure.
