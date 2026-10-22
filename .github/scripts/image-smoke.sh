#!/bin/sh
# Runs a paper replay inside the image the way an operator would, and checks it.
# The strategy is a user strategy from a mounted directory, loaded by import path.
# Its market order must fill, then TERM must stop the engine with exit 0.
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

# The same config and mounts as compose.example.yaml, so CI also checks the example.
# Only this env file reaches the engine. Docker passes no host environment.
docker run -d --name "$name" ${platform:+--platform "$platform"} \
  -v "$root/examples/ticks.jsonl:/replay/ticks.jsonl:ro" \
  -v "$root/examples/strategies:/strategies:ro" \
  --env-file "$root/examples/paper.env" \
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
