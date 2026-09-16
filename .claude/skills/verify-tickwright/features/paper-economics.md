# Paper economics

The paper venue charges the fees its specs declare, settles funding at the top of every UTC hour
at the per-boundary rate its specs declare, and can fill through a seeded stochastic model. All
three are inputs the operator sets in `.env`, and the ledger shows their effect in the store.

## Sub-features

- `econ-fees` a taker fee on every market fill lands on `positions.fees` and comes off cash.
- `econ-funding` a boundary crossed by the tick stream pays `size × mark × rate`, longs pay.
- `econ-stochastic` the same seed and ticks produce a byte-identical store.
- `econ-partial` a resting limit crossed by a tick fills in fractions and converges to `filled`.

## How to get to it (user POV)

- Set `maker_fee`, `taker_fee`, `funding_rate` inside `TICKWRIGHT_PAPER__INSTRUMENT_SPECS`.
- Set `TICKWRIGHT_PAPER__FILL_MODEL=stochastic`, `TICKWRIGHT_PAPER__SEED`, and the
  `TICKWRIGHT_PAPER__STOCHASTIC__*` knobs.

## Driving it with verify

Preconditions:

- Run ids `fees`, `fund`, `stoch` are unused.
- `SPECS_FEES='{"BTC":{"symbol":"BTC","sz_decimals":3,"max_decimals":6,"max_sig_figs":5,"min_notional":"10","max_leverage":20,"margin_maint":"0.02","maker_fee":"0.00015","taker_fee":"0.00045"}}'`
- `SPECS_FUND='{"BTC":{"symbol":"BTC","sz_decimals":3,"max_decimals":6,"max_sig_figs":5,"min_notional":"10","max_leverage":20,"margin_maint":"0.02","funding_rate":"0.0001"}}'`

- **Fees.** Run `$V init fees`,
  `$V ticks fees ticks --row BTC:42000@0 --row BTC:42100@1s --row BTC:42200@2s --row BTC:42300@3s`,
  `$V start fees rt --preset paper-replay --strategy round_trip_then_flat --param HOLD_TICKS=2 --env "TICKWRIGHT_PAPER__INSTRUMENT_SPECS=$SPECS_FEES"`,
  `$V await fees rt --event verify.flat`, `$V signal fees rt TERM`, `$V await fees rt --exit`,
  `$V dump fees rt`. Buy 0.5 at 42000, sell 0.5 at 42200. `positions` reads
  `realized_pnl 100.000`, `fees 18.94500000` (0.00045 × (21000 + 21100)). `account` cash reads
  `100081.05500000`.
- **Funding.** Run `$V init fund`,
  `$V ticks fund ticks --base 2024-01-01T00:59:00+00:00 --row BTC:40000@0 --row BTC:40000@30s --row BTC:40000@2m`,
  `$V start fund life --preset paper-replay --strategy margin_watch --param QUANTITY=1 --env "TICKWRIGHT_PAPER__INSTRUMENT_SPECS=$SPECS_FUND"`,
  `$V await fund life --sql "select funding from positions where strategy_id='margin_watch'" --expect -4.0000000`,
  `$V signal fund life TERM`, `$V await fund life --exit`, `$V dump fund life`. The 01:00 boundary
  pays `1 × 40000 × 0.0001 = 4`, a long pays, so `funding -4.0000000` and cash `99996.0000000`.
  `funding_marks` reads `BTC | 1704070800000000000` (01:00 UTC).
- **Stochastic, twice.** Run `$V init stoch`,
  `$V ticks stoch ticks --row BTC:42050@0 --row BTC:42010@1s --row BTC:42000@2s --row BTC:41990@3s --row BTC:41980@4s --row BTC:41970@5s`.
  Then for each life `a` and `b`: `$V start stoch <life> --preset paper-replay --env 'TICKWRIGHT_STRATEGIES=[{"kind":"single_shot_limit","strategy_id":"rester","symbol":"BTC","side":"buy","quantity":"1","price":"42020"}]' --env TICKWRIGHT_PAPER__FILL_MODEL=stochastic --env TICKWRIGHT_PAPER__SEED=7 --env TICKWRIGHT_PAPER__STOCHASTIC__PARTIAL_FILL_FRACTION=0.5`,
  `$V await stoch <life> --order $($V cloid rester:BTC:1)=FILLED`, `$V signal stoch <life> TERM`,
  `$V await stoch <life> --exit`, `$V dump stoch <life>`, and
  `rm .agents/verify/stoch/scratch/store.db` between the two.
- **Proof.** `diff .agents/verify/stoch/evidence/a.store.txt .agents/verify/stoch/evidence/b.store.txt`
  is empty. `a.events.txt` has at least one `order.partially_filled` before `order.filled`.
- **Cleanup.** Run `$V cleanup fees`, `$V cleanup fund`, `$V cleanup stoch`.

## Gotchas

- `funding_rate` is per boundary (hourly), not the venue's 8 hour headline. A boundary is only
  settled when a tick moves the clock past it. The last row must be after the boundary.
- `max_slippage` is a fraction of price, not a price. `5` means up to 500 percent. Use `0.01`.
- Partial fills apply to a resting limit only. A market order always fills in full, with slippage
  if enabled.
- The stochastic proof needs a fresh store for life `b`. Without the `rm` the second life recovers
  the first life's fill and places nothing.
