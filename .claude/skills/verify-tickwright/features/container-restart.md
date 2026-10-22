# Container restart

The engine runs from the published image with a user strategy mounted from outside the package and
named by import path. It is killed while it holds a position and a resting limit. The next start
clears the barrier, and the strategy rebuilds its state from `position()` and `open_orders()` in
`restore()`. It places no second order. This runs on both soak setups: SQLite with the in-memory
bus, and Postgres with Kafka. A strategy path that does not import exits `2`, and the example
compose stops after 3 restarts (#534).

## Sub-features

- `a-resume` Setup A, SQLite and in-memory bus: after the `KILL` the restart clears the barrier,
  the strategy reads its resting order, and no order is placed.
- `a-position` Setup A: the position after the restart is the position before the kill.
- `b-resume` Setup B, Postgres and Kafka: the same as `a-resume`.
- `b-position` Setup B: the same as `a-position`.
- `boot-refusal` a container whose strategy does not import exits `2`, and compose stops it after
  3 restarts.

## How to get to it (user POV)

- `IMAGE_TAG=sha-<commit> docker compose -f compose.example.yaml up`, with your strategy in the
  mounted directory and named in `TICKWRIGHT_STRATEGIES`.
- `docker kill <container>` while it holds a position and a resting order, then start it again.

## Driving it with verify

Preconditions:

- Docker is up, and run id `container` is unused.
- `IMG=ghcr.io/marcosach/tickwright:sha-$(git rev-parse main)` is pulled. Use the image of the
  commit you verify. The run never builds one.
- `S='[{"strategy":"rest_and_hold:RestAndHold","strategy_id":"holder","symbols":["BTC"],"settings":{"symbol":"BTC","quantity":"0.5","rest_price":"40000"}}]'`.
  The strategy is `../strategies/rest_and_hold.py`. The limit at 40000 rests below every tick.

Steps:

- **Set up.** Run `$V init container --feature container-restart` and
  `$V ticks container ticks --row BTC:42000@0 --row BTC:42100@1s`.
- **Setup A, life one.** Run
  `$V start container a1 --preset paper-replay --image $IMG --env "TICKWRIGHT_STRATEGIES=$S"`,
  `$V await container a1 --sql "select count(*) from orders where state='live'" --expect 1`,
  `$V check container a1 a-position --sql "select signed_size from positions where strategy_id='holder'" --expect 0.500`,
  `$V signal container a1 KILL`, `$V await container a1 --exit`, `$V dump container a1`,
  `$V check container a1 a-resume --exit --expect 137`.
- **Setup A, life two.** Run the same `start` line with life `a2`,
  `$V await container a2 --event verify.restored`, wait three seconds,
  `$V signal container a2 TERM`, `$V await container a2 --exit`, `$V dump container a2`.
- **Check Setup A.** Run
  `$V check container a2 a-resume --event engine.barrier_cleared --expect 1`,
  `$V check container a2 a-resume --event verify.restored --expect 1`,
  `$V check container a2 a-resume --event order.placed --expect 0`,
  `$V check container a2 a-resume --sql "select count(*) from orders where state='live'" --expect 1`,
  `$V check container a2 a-resume --exit --expect 0`,
  `$V check container a2 a-position --sql "select signed_size from positions where strategy_id='holder'" --expect 0.500`.
- **Setup B, infra.** Run `$V infra container up postgres` and `$V infra container up kafka`.
  Then set `K=$(docker compose ps -q kafka)` from the repo root, and
  `B=(--preset paper-replay --image $IMG --network container:$K --env TICKWRIGHT_STORE=postgres --env TICKWRIGHT_POSTGRES__DSN=postgresql://tickwright:tickwright@localhost:5432/verify_container --container-env TICKWRIGHT_POSTGRES__DSN=postgresql://tickwright:tickwright@postgres:5432/verify_container --env TICKWRIGHT_BUS=kafka --env TICKWRIGHT_KAFKA__EVENTS_TOPIC=verify.container --env TICKWRIGHT_KAFKA__GROUP_ID=verify.container --env "TICKWRIGHT_STRATEGIES=$S")`.
- **Setup B, life one.** Run `$V start container b1 "${B[@]}"`, then the Setup A life one steps
  with life `b1` and check ids `b-position` and `b-resume`.
- **Setup B, life two.** Run `$V start container b2 "${B[@]}"`, then the Setup A life two and
  check steps with life `b2` and check ids `b-resume` and `b-position`.
- **Boot refusal.** Write `scratch/refuse.yaml` with one service `tickwright` whose `environment`
  sets `TICKWRIGHT_STRATEGIES` to
  `[{"strategy":"no_such_module:Nope","strategy_id":"bad","symbols":["BTC"],"settings":{}}]`. Run
  `IMAGE_TAG=sha-$(git rev-parse main) docker compose -p verify-container -f compose.example.yaml -f .agents/verify/container/scratch/refuse.yaml up -d`.
  Wait until `docker inspect -f '{{.State.Status}} {{.State.ExitCode}} {{.RestartCount}}' verify-container-tickwright-1`
  prints `exited 2 3`, then wait ten more seconds. Write that line to
  `evidence/refusal.state.txt` and `docker logs verify-container-tickwright-1` to
  `evidence/refusal.logs.txt`. Run
  `$V check container refusal boot-refusal --file refusal.state.txt --expect "exited 2 3"`.
  Then run the same compose command with `down -v` in place of `up -d`.
- **Report.** Run `$V report container`. Expected verdict: `PASS`.
- **Cleanup.** Run `$V cleanup container`.

## Gotchas

- A container `KILL` exits `137`, not `-9`. The helper's pid is the `docker run` client. `signal`
  sends to the container instead, because a `KILL` to the client leaves the engine running.
- The Kafka broker advertises `localhost:9092`, so the engine joins the broker's network. There
  Postgres is `postgres:5432`. The helper still reads the store from the host at `localhost:5432`,
  so the container gets its own DSN through `--container-env`.
- A resting limit reads `live` in the `orders` table, not `resting`.
- `verify.restored` is logged by the strategy, not the engine. It shows what `restore()` read.
- The refusal uses its own compose project, so its named volume never mixes with a real one.
- Compose counts restarts over the container's whole life. `refusal.logs.txt` holds one refusal
  line per attempt, so 4 lines.
