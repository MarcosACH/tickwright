# Kafka bus

The same paper replay run, with every event crossing a real Kafka broker instead of the in-memory
bus. The proof is the fill in the store plus the messages counted on the run's own topic.

## Sub-features

- `kafka-boot` the engine connects, clears the barrier, and starts the feed on `KafkaBus`.
- `kafka-fill` the market order fills through the broker round trip.
- `kafka-topic` the run's topic holds the events (offsets above zero), and nothing else's.

## How to get to it (user POV)

- `docker compose up -d kafka`, then `TICKWRIGHT_BUS=kafka uv run tickwright`.

## Driving it with verify

Preconditions:

- `$V doctor` says the Docker daemon is up. On this machine that is OrbStack (`orb start`).
- Run id `kafka` is unused.

- **Infra.** Run `$V init kafka` and `$V infra kafka up kafka`. It waits for the healthcheck and
  prints the topic name `verify.kafka`.
- **Run.** Run `$V ticks kafka ticks --row BTC:42000@0 --row BTC:42100@1s`,
  `$V start kafka first --preset paper-replay --env TICKWRIGHT_BUS=kafka --env TICKWRIGHT_KAFKA__EVENTS_TOPIC=verify.kafka --env TICKWRIGHT_KAFKA__GROUP_ID=verify.kafka --env 'TICKWRIGHT_STRATEGIES=[{"kind":"single_shot_market","strategy_id":"shooter","symbol":"BTC","side":"buy","quantity":"0.5"}]'`,
  `$V await kafka first --order $($V cloid shooter:BTC:1)=FILLED --timeout 60`.
- **Count.** Run
  `docker compose exec kafka /opt/kafka/bin/kafka-get-offsets.sh --bootstrap-server localhost:9092 --topic verify.kafka`.
  It prints `verify.kafka:0:9` for this tick file (two marks, two ticks, and the order saga).
  Save the line: `... > .agents/verify/kafka/evidence/first.offsets.txt`.
- **Stop.** Run `$V signal kafka first TERM`, `$V await kafka first --exit --timeout 30`,
  `$V dump kafka first`.
- **Proof.** Exit code `0`. `orders` shows `filled`. `first.offsets.txt` shows a non-zero offset.
- **Cleanup.** Run `$V cleanup kafka`. It stops and removes the broker because this run started
  it.

## Gotchas

- The first Kafka boot is slower. Give `--timeout 60` on the first await.
- Topic and group id must be unique per run. Two engines on one topic collide on unqualified
  symbols. The helper does not enforce this, the recipe does.
- The broker is `tmpfs`. Stopping it drops every topic. That is the isolation, not a loss.
- If Docker is down, report `verified-unreachable` with the `doctor` line, do not skip silently.
