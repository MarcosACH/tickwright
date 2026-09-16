# Hyperliquid feed

The public mainnet trade stream drives the paper venue. No key, no funds, no order leaves the
process. This proves the WebSocket adapter, the wall clock, ingress conflation, and a paper fill
at a real market price.

## Sub-features

- `hlfeed-connect` the feed connects and `engine.feed_started` fires within seconds.
- `hlfeed-fill` a market order fills at the latest mainnet trade price.
- `hlfeed-lag` `feed.lagged` lines appear when the stream conflates. Informational.

## How to get to it (user POV)

- `TICKWRIGHT_FEED=hyperliquid TICKWRIGHT_HYPERLIQUID__SYMBOLS='["BTC"]' uv run tickwright`.

## Driving it with verify

Preconditions:

- Network access to `wss://api.hyperliquid.xyz/ws`.
- Run id `livefeed` is unused.

- **Run.** Run `$V init livefeed`,
  `$V start livefeed first --preset paper-livefeed --env 'TICKWRIGHT_STRATEGIES=[{"kind":"single_shot_market","strategy_id":"shooter","symbol":"BTC","side":"buy","quantity":"0.01"}]'`,
  `$V await livefeed first --event engine.feed_started --timeout 30`,
  `$V await livefeed first --order $($V cloid shooter:BTC:1)=FILLED --timeout 90`.
- **Stop.** Run `$V signal livefeed first TERM`, `$V await livefeed first --exit`,
  `$V dump livefeed first`.
- **Proof.** `positions` shows `shooter | BTC | 0.010 | <price>` where the price is the current
  BTC market, and `ts_ns` is a real timestamp (about 1.79e18 in 2026), not `1704067200...`.
  Exit code `0`. `first.env.txt` has `TICKWRIGHT_HYPERLIQUID__TESTNET=false` and no
  `TICKWRIGHT_EXCHANGE` line, so the venue was paper.
- **Cleanup.** Run `$V cleanup livefeed`.

## Gotchas

- The first tick can take up to a minute on a quiet market. `--timeout 90` on the fill.
- This is mainnet data. It is read only. The preset never sets `TICKWRIGHT_EXCHANGE`, and you
  must not add it.
- Reconciliation cadences run on the wall clock here. A resting limit left `LIVE` would be
  re-read every 30 seconds for real. Stop the process when the proof is in.
