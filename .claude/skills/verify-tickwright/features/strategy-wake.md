# Strategy wake-ups

A strategy can wake on a clock grid, even when ticks stop (ADR-0062). The reference limit strategy
uses it to cancel its resting order after N seconds. This recipe proves the cancel with no tick on
the order's symbol, and proves that wake-ups stay on the grid across a crash and a restart.

## Sub-features

- `wake-cancel` the resting BTC limit is cancelled after `cancel_after_seconds`, with no BTC tick
  after it was placed. ETH rows move the replay clock.
- `wake-deadline` the same order stays `LIVE` when replay time stops before the deadline.
- `wake-grid` every wake slot is a whole second, before and after a `KILL`. Slots missed while
  the engine was down are skipped, not caught up.
- `wake-restart` the second life places nothing and exits 0.
- `wake-kafka` on `KafkaBus` the same lives pass the same checks, and every `Wake` record on the
  topic is on the grid.

## How to get to it (user POV)

- Add `"cancel_after_seconds": 3` to a `SingleShotLimitStrategy` entry's `settings` in
  `TICKWRIGHT_STRATEGIES`, then `uv run tickwright`. The order is cancelled 3 seconds after its
  placing tick, whether or not more ticks arrive.

## Driving it with verify

Preconditions:

- `$V doctor` says the package imports.
- Run ids `wake` and `wakectl` are unused.
- `S='[{"strategy":"tickwright.strategies.single_shot_limit:SingleShotLimitStrategy","strategy_id":"timer","symbols":["BTC"],"settings":{"side":"buy","quantity":"0.5","price":"40000","cancel_after_seconds":3}},{"strategy":"wake_recorder:WakeRecorder","strategy_id":"recorder","symbols":["ETH"],"settings":{}}]'`
- `REST=$($V cloid timer:BTC:1)`
- `SLOTS="select group_concat((value - 1704067200000000000) / 1000000000) from strategy_snapshots, json_each(cast(data as text), '\$.slots') where strategy_id = 'recorder'"`
- `OFFGRID="select count(*) from strategy_snapshots, json_each(cast(data as text), '\$.slots') where strategy_id = 'recorder' and value % 1000000000 != 0"`

- **Set up.** Run `$V init wake --feature strategy-wake`, `$V init wakectl --feature strategy-wake`,
  `$V ticks wake ticks --row BTC:42000@300ms --row ETH:2500@1300ms --row ETH:2500@2300ms --row ETH:2500@3300ms --row ETH:2500@4300ms --row ETH:2500@5300ms`,
  `$V ticks wake later --row ETH:2500@10700ms --row ETH:2500@11700ms --row ETH:2500@12700ms --row ETH:2500@13700ms`
  and `$V ticks wakectl ticks --row BTC:42000@300ms --row ETH:2500@1300ms --row ETH:2500@2300ms`.
- **Life one.** Run `$V start wake first --preset paper-replay --env "TICKWRIGHT_STRATEGIES=$S"`,
  `$V await wake first --order $REST=CANCELLED`, wait two seconds, `$V signal wake first KILL`,
  `$V await wake first --exit`, `$V dump wake first`.
- **Check life one.** Run
  `$V check wake first wake-cancel --sql "select state from orders where cloid='$REST'" --expect cancelled`,
  `$V check wake first wake-cancel --exit --expect -9`,
  `$V check wake first wake-grid --sql "$SLOTS" --expect 0,1,2,3,4,5`.
- **Life two.** Run
  `$V start wake second --preset paper-replay --env TICKWRIGHT_REPLAY__PATH=later.jsonl --env "TICKWRIGHT_STRATEGIES=$S"`,
  `$V await wake second --event engine.feed_started`, wait three seconds,
  `$V signal wake second TERM`, `$V await wake second --exit`, `$V dump wake second`.
- **Check life two.** Run
  `$V check wake second wake-grid --sql "$SLOTS" --expect 0,1,2,3,4,5,10,11,12,13`,
  `$V check wake second wake-grid --sql "$OFFGRID" --expect 0`,
  `$V check wake second wake-restart --event order.placed --expect 0`,
  `$V check wake second wake-restart --exit --expect 0`.
- **Control.** Run `$V start wakectl first --preset paper-replay --env "TICKWRIGHT_STRATEGIES=$S"`,
  `$V await wakectl first --order $REST=LIVE`, wait three seconds, `$V signal wakectl first TERM`,
  `$V await wakectl first --exit`, `$V dump wakectl first`, then
  `$V check wakectl first wake-deadline --sql "select state from orders where cloid='$REST'" --expect live`,
  `$V check wakectl first wake-deadline --event order.cancelled --expect 0`.
- **Kafka.** Needs the Docker daemon up and run id `wakek` unused. Run
  `$V init wakek --feature strategy-wake`, `$V infra wakek up kafka`, and the same two `ticks`
  lines as `wake`. Then repeat **Life one** to **Check life two** with `wakek` in place of `wake`.
  Add `--env TICKWRIGHT_BUS=kafka --env TICKWRIGHT_KAFKA__EVENTS_TOPIC=verify.wakek --env TICKWRIGHT_KAFKA__GROUP_ID=verify.wakek`
  to both `start` lines, and `--timeout 60` to the first `await` of each life. Wait five seconds
  in life two, not three.
- **Kafka topic.** Run
  `docker compose exec -T kafka /opt/kafka/bin/kafka-console-consumer.sh --bootstrap-server localhost:9092 --topic verify.wakek --from-beginning --timeout-ms 5000 --property print.key=true > .agents/verify/wakek/evidence/second.topic.txt`,
  `grep -c '"type":"Wake"' .agents/verify/wakek/evidence/second.topic.txt > .agents/verify/wakek/evidence/second.wakes.txt`,
  `grep '"type":"Wake"' .agents/verify/wakek/evidence/second.topic.txt | grep -vc '"ts_event":[0-9]*000000000,' > .agents/verify/wakek/evidence/second.wakes-offgrid.txt`,
  then `$V check wakek second wake-kafka --file second.wakes.txt --expect 20` and
  `$V check wakek second wake-kafka --file second.wakes-offgrid.txt --expect 0`.
- **Report.** Run `$V report wake`, `$V report wakectl` and `$V report wakek`. Expected verdicts:
  `PASS (0 of 7 checks failed)`, `PASS (0 of 2 checks failed)` and `PASS (0 of 9 checks failed)`.
- **Cleanup.** Run `$V cleanup wake`, `$V cleanup wakectl` and `$V cleanup wakek`.

## Gotchas

- Replay time moves only when a row is read. When the file ends, time stops and no wake fires. So
  the ETH rows are what carry time past the deadline. The order's own symbol gets no tick.
- The deadline is the placing tick's time plus N seconds. Here that is 3.3 s. The first wake at or
  past it is the 4 s slot, so the ETH row at 4.3 s is the one that fires the cancel.
- Log timestamps are wall time, not replay time. That is why the control run proves the deadline,
  not a log line.
- `wake_recorder:WakeRecorder` lives in `../strategies/`. The helper puts that directory on the
  venv life's `PYTHONPATH`, so it loads by import path as in a container life. The engine saves its
  snapshot after every `on_wake`, so the slot list survives the `KILL`.
- The recorder trades ETH because two strategies cannot own one symbol. It ignores its ticks.
- Life two reads `later.jsonl`, whose rows are 0.7 s past a whole second. A loop that counted from
  its start, not from the grid, would show off-grid slots there. A second life on the same file
  would move replay time backwards.
- `SLOTS` prints seconds past the default `--base` of `2024-01-01`. Integer division hides an
  off-grid slot, so `OFFGRID` checks the grid itself.
- The topic holds 20 `Wake` records: 10 slots for each of the two strategies. Each strategy gets a
  `Wake`, even the one that has nothing left to do. The record key is the `strategy_id`.
- In zsh, a variable holding several `--env` flags is not split into words. Write the flags out.
- The control is its own run id. `check --sql` reads the store named in the run's `scratch/.env`,
  and each `start` rewrites that file.
