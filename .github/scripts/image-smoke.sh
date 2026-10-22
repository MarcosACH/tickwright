#!/bin/sh
# Runs compose.example.yaml with a local image, the way an operator would, and checks it.
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
project=tickwright-smoke-$$
# Python under QEMU starts slowly, so the wait is generous.
timeout=180

# The compose loads .env.secrets when it exists. A key there must never reach a smoke run.
if [ -e "$root/.env.secrets" ]; then
  echo "smoke: $root/.env.secrets exists. Move it away, the smoke must run with no key." >&2
  exit 1
fi

# The compose names its image by tag. This tag points at the local image under test.
export IMAGE_TAG=smoke-$$
# Pins the platform, so a mismatch fails instead of running another image.
if [ -n "$platform" ]; then
  export DOCKER_DEFAULT_PLATFORM="$platform"
fi
# /dev/null keeps a developer .env out of the compose variables.
compose() { docker compose --env-file /dev/null -p "$project" -f "$root/compose.example.yaml" "$@"; }
ref=$(compose config --images)
id=

cleanup() {
  compose down -v >/dev/null 2>&1 || true
  docker rmi "$ref" >/dev/null 2>&1 || true
}
trap cleanup EXIT

fail() {
  echo "smoke: $1" >&2
  if [ -n "$id" ]; then docker logs "$id" >&2 || true; fi
  exit 1
}

docker tag "$image" "$ref"
# `never` makes sure the run uses the local image, not one from GHCR.
if ! out=$(compose up -d --pull never 2>&1); then
  fail "compose up failed: $out"
fi
id=$(compose ps -aq tickwright)

waited=0
until docker logs "$id" 2>&1 | grep -q '"event": "order.filled"'; do
  # The compose restarts on failure, so a fault can look like a running engine.
  # A restart count above 0 means it exited at least once.
  state=$(docker inspect -f '{{.State.Status}} {{.RestartCount}}' "$id")
  if [ "$state" != "running 0" ]; then
    fail "the engine exited before an order filled ($state)"
  fi
  if [ "$waited" -ge "$timeout" ]; then
    fail "no order filled after ${timeout}s"
  fi
  sleep 1
  waited=$((waited + 1))
done

# Uses the compose stop_grace_period, so that setting is checked too.
if ! out=$(compose stop 2>&1); then
  fail "compose stop failed: $out"
fi
code=$(docker inspect -f '{{.State.ExitCode}}' "$id")
if [ "$code" != 0 ]; then
  fail "TERM gave exit $code, want 0"
fi
echo "smoke: ok ($image${platform:+ $platform})"
