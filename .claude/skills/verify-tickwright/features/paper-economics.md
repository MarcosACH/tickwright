# Paper economics

The paper venue charges the fees its specs declare, settles funding at the top of every UTC hour
at the per-boundary rate its specs declare, and can fill through a seeded stochastic model. All
three are inputs the operator sets in `.env`, and the ledger shows their effect in the store.

## Sub-features

- `econ-fees` a taker fee on every market fill lands on `positions.fees` and comes off cash.
- `econ-funding` a boundary crossed by the tick stream pays `size × mark × rate`, longs pay.
- `econ-stochastic` the same seed and ticks produce the same slipped fill price, pinned.
- `econ-partial` a resting limit crossed by a tick fills in fractions and converges to `filled`.

## How to get to it (user POV)

- Set `maker_fee`, `taker_fee`, `funding_rate` inside `TICKWRIGHT_PAPER__INSTRUMENT_SPECS`.
- Set `TICKWRIGHT_PAPER__FILL_MODEL=stochastic`, `TICKWRIGHT_PAPER__SEED`, and the
  `TICKWRIGHT_PAPER__STOCHASTIC__*` knobs.

## Driving it with verify

Preconditions:

- Run ids `fees`, `fund`, `stoch` are unused. One run per sub-feature, so a FAIL names its own run.
- `SPECS_FEES='{"BTC":{"symbol":"BTC","sz_decimals":3,"max_decimals":6,"max_sig_figs":5,"min_notional":"10","max_leverage":20,"margin_maint":"0.02","maker_fee":"0.00015","taker_fee":"0.00045"}}'`
- `SPECS_FUND='{"BTC":{"symbol":"BTC","sz_decimals":3,"max_decimals":6,"max_sig_figs":5,"min_notional":"10","max_leverage":20,"margin_maint":"0.02","funding_rate":"0.0001"}}'`

- **Fees.** Run `$V init fees --feature paper-economics`,
  `$V ticks fees ticks --row BTC:42000@0 --row BTC:42100@1s --row BTC:42200@2s --row BTC:42300@3s`,
  `$V start fees rt --preset paper-replay --strategy round_trip_then_flat --param HOLD_TICKS=2 --env "TICKWRIGHT_PAPER__INSTRUMENT_SPECS=$SPECS_FEES"`,
  `$V await fees rt --event verify.flat`, `$V signal fees rt TERM`, `$V await fees rt --exit`,
  `$V dump fees rt`. Buy 0.5 at 42000, sell 0.5 at 42200, taker 0.00045 on both notionals.
  Check: `$V check fees rt econ-fees --sql "select realized_pnl from positions" --expect 100.000`,
  `$V check fees rt econ-fees --sql "select fees from positions" --expect 18.94500000`,
  `$V check fees rt econ-fees --sql "select cash from account" --expect 100081.05500000`.
  Then `$V report fees`, expected `PASS (0 of 3 checks failed)`, and `$V cleanup fees`.
- **Funding.** Run `$V init fund --feature paper-economics`,
  `$V ticks fund ticks --base 2024-01-01T00:59:00+00:00 --row BTC:40000@0 --row BTC:40000@30s --row BTC:40000@2m`,
  `$V start fund life --preset paper-replay --strategy margin_watch --param QUANTITY=1 --env "TICKWRIGHT_PAPER__INSTRUMENT_SPECS=$SPECS_FUND"`,
  `$V await fund life --sql "select funding from positions" --expect -4.0000000`,
  `$V signal fund life TERM`, `$V await fund life --exit`, `$V dump fund life`. The 01:00
  boundary pays `1 × 40000 × 0.0001 = 4`, a long pays. Check:
  `$V check fund life econ-funding --sql "select funding from positions" --expect -4.0000000`,
  `$V check fund life econ-funding --sql "select cash from account" --expect 99996.0000000`,
  `$V check fund life econ-funding --sql "select last_funding_ts_ns from funding_marks" --expect 1704070800000000000`.
  Then `$V report fund`, expected `PASS (0 of 3 checks failed)`, and `$V cleanup fund`.
- **Stochastic.** Run `$V init stoch --feature paper-economics`,
  `$V ticks stoch ticks --row BTC:42050@0 --row BTC:42010@1s --row BTC:42000@2s --row BTC:41990@3s --row BTC:41980@4s --row BTC:41970@5s`.
  For each life `a` then `b`:
  `$V start stoch <life> --preset paper-replay --env 'TICKWRIGHT_STRATEGIES=[{"kind":"single_shot_market","strategy_id":"shooter","symbol":"BTC","side":"buy","quantity":"1"}]' --env TICKWRIGHT_PAPER__FILL_MODEL=stochastic --env TICKWRIGHT_PAPER__SEED=7 --env TICKWRIGHT_PAPER__STOCHASTIC__PROB_SLIPPAGE=1.0 --env TICKWRIGHT_PAPER__STOCHASTIC__MAX_SLIPPAGE=0.01`,
  `$V await stoch <life> --order $($V cloid shooter:BTC:1)=FILLED`, `$V signal stoch <life> TERM`,
  `$V await stoch <life> --exit`, `$V dump stoch <life>`,
  `$V check stoch <life> econ-stochastic --sql "select entry_price from positions" --expect 42113.4320776352530573600`,
  then `rm .agents/verify/stoch/scratch/store.db`. The pinned price is seed 7's first draw on
  42050, and it must come out the same in both lives and on any machine.
- **Partial.** In the same run:
  `$V start stoch partial --preset paper-replay --env 'TICKWRIGHT_STRATEGIES=[{"kind":"single_shot_limit","strategy_id":"rester","symbol":"BTC","side":"buy","quantity":"1","price":"42020"}]' --env TICKWRIGHT_PAPER__FILL_MODEL=stochastic --env TICKWRIGHT_PAPER__SEED=7 --env TICKWRIGHT_PAPER__STOCHASTIC__PARTIAL_FILL_FRACTION=0.5`,
  `$V await stoch partial --order $($V cloid rester:BTC:1)=FILLED`, `$V signal stoch partial TERM`,
  `$V await stoch partial --exit`, `$V dump stoch partial`. Check:
  `$V check stoch partial econ-partial --event order.partially_filled --expect 1`,
  `$V check stoch partial econ-partial --sql "select cum_qty from orders" --expect 1.0000`.
- **Report.** Run `$V report stoch`. Expected verdict: `PASS (0 of 4 checks failed)`.
- **Cleanup.** Run `$V cleanup stoch`.

## Gotchas

- `funding_rate` is per boundary (hourly), not the venue's 8 hour headline. A boundary is only
  settled when a tick moves the clock past it. The last row must be after the boundary.
- `max_slippage` is a fraction of price, not a price. `5` means up to 500 percent. Use `0.01`.
- Partial fills apply to a resting limit only. A market order always fills in full, with slippage
  if enabled.
- The stochastic proof needs a fresh store for life `b`. Without the `rm` the second life recovers
  the first life's fill and places nothing.
