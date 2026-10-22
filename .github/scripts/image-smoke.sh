#!/bin/sh
# Runs a paper replay inside the image the way an operator would, and checks it.
# A market order must fill, then TERM must stop the engine with exit 0.
# A replay does not exit when its file ends, so this script sends the stop.
# It needs no key and no external service. CI runs it before anything is published.
#
# Usage: image-smoke.sh IMAGE [PLATFORM]
set -eu

image=$1
platform=${2:-}
root=$(cd "$(dirname "$0")/../.." && pwd)
name=tickwright-smoke-$$
# Python under QEMU starts slowly, so the wait is generous.
timeout=180

cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT

fail() {
  echo "smoke: $1" >&2
  docker logs "$name" >&2 || true
  exit 1
}

# Only the variables set here reach the engine. Docker passes no host environment.
docker run -d --name "$name" ${platform:+--platform "$platform"} \
  -v "$root/examples/ticks.jsonl:/replay/ticks.jsonl:ro" \
  -e TICKWRIGHT_REPLAY__PATH=/replay/ticks.jsonl \
  -e TICKWRIGHT_PAPER__GENESIS_COLLATERAL=100000 \
  -e 'TICKWRIGHT_PAPER__INSTRUMENT_SPECS={"BTC": {"symbol": "BTC", "sz_decimals": 3, "max_decimals": 6, "max_sig_figs": 5, "min_notional": "10"}}' \
  -e 'TICKWRIGHT_STRATEGIES=[{"strategy": "tickwright.strategies.single_shot:SingleShotMarketStrategy", "strategy_id": "smoke", "symbols": ["BTC"], "settings": {"side": "buy", "quantity": "0.5"}}]' \
  "$image" >/dev/null

waited=0
until docker logs "$name" 2>&1 | grep -q '"event": "order.filled"'; do
  if [ "$(docker inspect -f '{{.State.Running}}' "$name")" != true ]; then
    fail "the engine exited before an order filled"
  fi
  if [ "$waited" -ge "$timeout" ]; then
    fail "no order filled after ${timeout}s"
  fi
  sleep 1
  waited=$((waited + 1))
done

docker stop -t 30 "$name" >/dev/null
code=$(docker inspect -f '{{.State.ExitCode}}' "$name")
if [ "$code" != 0 ]; then
  fail "TERM gave exit $code, want 0"
fi
echo "smoke: ok ($image${platform:+ $platform})"
