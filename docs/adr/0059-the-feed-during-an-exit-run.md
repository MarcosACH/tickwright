# The feed during an exit run

A market order needs a cached trade price, on paper and on Hyperliquid. Without one, both adapters
raise. An exit run (ADR-0052) starts no strategy, so it was open whether it starts the feed at all.
Flatten sends market orders and retries after a miss (ADR-0056). So it needs a price, and the price
must update between attempts. This ADR decides when an exit run starts the feed, and at what price
a paper flatten fills. Decided in [#438](https://github.com/MarcosACH/tickwright/issues/438), part
of [#408](https://github.com/MarcosACH/tickwright/issues/408).

## Decision

- **`tickwright flatten` starts the feed.** It brings both the last trade and the mark (ADR-0039).
- **A market order never goes past the bound around the mark.** A buy goes out at the lower of
  last trade × (1 + `SLIPPAGE_BOUND`) and mark × (1 + `SLIPPAGE_BOUND`). A sell goes out at the
  higher of the two, with (1 − `SLIPPAGE_BOUND`). This holds for every market order on
  Hyperliquid, not only flatten, so the adapter keeps one price rule (#431). A missing mark counts
  as a missing price. Paper is unchanged. It still fills at the last tick.
- **The live feed also subscribes to every symbol the account holds.** Flatten covers positions the
  engine did not open (ADR-0052). Those can be on coins the operator never configured. The run
  reads the venue position at boot, so it knows these symbols before the feed starts.
- **Under replay, the exit run plays the whole file first.** Flatten then fills at the last row's
  price. The fill is dated at the end of the file. A live feed never ends, so flatten places as
  soon as each symbol has a price.
- **`tickwright cancel-all` starts no feed.** A cancel needs no price. Flatten runs cancel all as
  its first step, before the feed starts.
- **A missing price counts as an attempt that fills nothing.** Flatten waits 2 seconds and tries
  again. After 3 in a row, it gives up and exits with an error (ADR-0056). This covers a replay
  file with no rows for a held symbol, and a WebSocket that cannot connect.

## Considered options

- **Read the price over REST before each attempt.** It works for any coin with no feed. But the read
  gives the mid, not the last trade. That would give flatten a second price rule, which #431 chose
  not to have.
- **Place as soon as a price arrives, under replay too.** Replay plays the file as fast as it can.
  The fill would land on whatever row the feed had reached, which depends on task timing. Tests
  could not pin the price.
- **Store how far the replay got, and resume from there.** It gives a closer price. It adds a new
  stored value and new code for a paper-only gain.
- **Refuse flatten under replay.** Replay is the default paper path. The hermetic path would have
  no flatten, and paper and live would differ.
- **Price from the last trade alone.** A stale trade can widen the bound. Say a short is held, the
  last trade is 100, and the mark has fallen to 96. A buy at 105 is about 9% above the market, not
  5%. On a thin book a full-size order can fill that far up.
- **Skip the attempt when the trade and the mark are far apart.** It needs a new limit setting. On a
  quiet coin the trade price stays old, so every attempt is skipped. Flatten could never close it.
- **Clamp only flatten orders.** The adapter would need a rule for one kind of order. #431 chose
  one rule for every market order.
- **Wait for a price with no time limit.** A dead connection would hang the run until the operator
  stops it by hand. The give-up rule already ends a run that cannot fill.

## Consequences

- On subscribe, the Hyperliquid trades stream sends the last 30 trades at once. A public mainnet
  check on 2026-10-07 showed this for BTC and for ZETA. So a live flatten normally has a price in
  under a second. The `activeAssetCtx` stream also sends the mark at once. The same check saw it in
  under half a second, for both coins.
- A quiet coin's last trade can be minutes old. The ZETA snapshot was 5 minutes old. The mark keeps
  the order within `SLIPPAGE_BOUND` of the market. An old trade on the far side of the mark makes
  the order tighter, so the attempt may fill nothing. No new trade means no new price, so flatten
  gives up. The operator can run it again.
- A strategy's market order also gets the clamp. Its fills can only get closer to the mark. It now
  also fails when no mark is cached.
- Not built yet. The build updates the `SLIPPAGE_BOUND` line in `.env.example` and the adapter's
  docstring, which still say last trade × (1 ± bound).
- Under replay, a missing price cannot hang the run. `ManualClock.sleep` returns at once and moves
  virtual time forward. So the 3 attempts end at once, and so does a stochastic fill's latency.
- A paper flatten under replay fills at the last price in the file, not at a current price. This is
  a known gap of paper replay.
- A replay exit run plays the whole file. On a long recording, the run takes longer to start
  flatten.
- The positions stay open while the file plays, so paper funding charges them up to the end of the
  file. The watermark in ADR-0043 §5.1 skips every hour a past run already charged. So nothing is
  charged twice. Only a run that stopped mid-file gets new charges.
- Under replay, `tickwright cancel-all` starts no feed, so virtual time never leaves 0. Its cancel
  records are dated 1970-01-01. Boot reconcile in a normal replay run is dated the same way. Flatten
  runs cancel all at the same point, so its cancels carry this date too.
- Paper with the live Hyperliquid feed behaves like live. It places as soon as each symbol has a
  price.
