# Hyperliquid testnet probe: does a rate-limited request restart the 10 second window?

**Probe date:** 2026-10-08
**Venue:** Hyperliquid testnet (`https://api.hyperliquid-testnet.xyz`). Nothing was sent to mainnet.
**SDK:** `hyperliquid-python-sdk==0.24.0`, from the project `.venv`.
**Feeds:** ticket [#452](https://github.com/MarcosACH/tickwright/issues/452), parent map
[#408](https://github.com/MarcosACH/tickwright/issues/408).
**Follows:** P8 in [`hyperliquid-reduce-only-cancel-market-probes.md`](hyperliquid-reduce-only-cancel-market-probes.md).

Scope: facts only. This note does not choose a design.

Setup: the same agent wallet as the earlier probes. The account was flat with no open orders. Its
address budget was `nRequestsUsed` 846 of `nRequestsCap` 97457. We used it up with batch cancels of
made-up ETH oids, 200 per action, until `nRequestsUsed` was 97459. Each probe request was a
reduce-only IOC sell of 0.01 ETH on the flat account. When one gets through, the venue rejects it
with `Reduce only order would increase position. asset=4`, so nothing fills. Times are seconds from
the start of each run, taken when the request was sent.

## Short answers

| Question | Answer |
| --- | --- |
| Does a refused request restart the 10 second window? | **No.** One request per second still got one through about every 11 seconds. |
| Does a refused request use the budget? | **No.** 40 refusals left `nRequestsUsed` alone. Each request that got through added 1. |
| What does a refusal look like? | HTTP 200. The SDK does not raise. Top-level `"status": "err"` with the text below. |
| Do loops that retry out of step share the slot fairly? | **No.** With 3 loops, one loop got nothing in 90 seconds. The other two got 5 each. |

The refusal body, copied from the raw response:

```json
{"status": "err", "response": "Too many cumulative requests sent (97460 > 97456) for cumulative volume traded $87457.46. Place taker orders to free up 1 request per USDC traded."}
```

A refusal came back in about 0.33 seconds. A request that got through came back in about 1.05
seconds.

## E1. One request every second for 45 seconds

45 requests. 5 got through, at 0.34, 11.50, 22.59, 33.67, and 44.78. The other 40 were refused.
The gaps are 11.1 seconds. Each pass came about 10 seconds after the previous pass was answered.
The 1 second spacing places the window to within 1 second, no closer.

If a refusal restarted the window, nothing would have got through after the first pass.

`nRequestsUsed` went from 97459 to 97464. That is one per pass and none per refusal. The number
inside the refusal text rose the same way (97460, 97461, ...). The right-hand number stayed 97456.

## E2. Control: one request every 12 seconds

After 12 seconds idle, 4 requests at about 12 second gaps. All 4 got through. `nRequestsUsed` went
from 97464 to 97468.

## E3. Three loops out of step, the flatten shape

Three threads, started 3.3 seconds apart. Each sends one request, then waits 10 seconds, whatever
the answer. This is close to ADR-0056: one loop per symbol, a 10 second wait after a rate limit.
Run for 90 seconds.

| Loop | Sent | Through | Times through |
| --- | --- | --- | --- |
| 0 | 9 | 5 | 12.3, 23.4, 34.4, 45.4, 56.5 |
| 1 | 9 | 5 | 57.0, 68.1, 79.1, 90.2, 101.2 |
| 2 | 9 | 0 | none |

The account as a whole got 10 requests through in about 90 seconds. So waiting made progress. But
no loop is promised a turn. Loop 0 held the slot first. Then loop 1 took it over. Loop 2 always
came just after another loop had used the slot.

Once, two requests went through 0.53 seconds apart (loop 0 at 56.47, loop 1 at 56.99). The earlier
passes were about 11 seconds apart. This note does not explain the double pass. It does not change
the answers above.

## Cleanup

The account ended 21 requests over its cap. A `reserveRequestWeight` action signed by the agent
wallet was refused 10 times with:

```json
{"status": "err", "response": "Must deposit before performing actions. User: <address>"}
```

It was not tried from the master wallet. Instead we bought 0.01 ETH with an IOC order and sold it
back. That traded $48.97. `nRequestsCap` rose from 97457 to 97506, which is 1 request per USDC as
the refusal text says. The account ended flat with no open orders. Test funds went from 922.30 to
922.27 USDC.

The account now has about 26 requests of headroom, against about 96,600 before this probe. A
testnet run that sends more than that will be rate limited. Trading volume or a
`reserveRequestWeight` from the master wallet raises it again.
