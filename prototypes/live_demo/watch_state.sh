#!/bin/sh
# Polls the scratch paper DB every 2s so you can watch the account/position
# update as RoundTripLoopStrategy's buys and sells fill. Read-only: never
# touches the real tickwright.db. Run: sh prototypes/live_demo/watch_state.sh
DB=${1:-/tmp/tickwright-roundtrip-demo.db}
while true; do
  clear
  echo "--- account ---"
  sqlite3 -header -column "$DB" "select * from account;"
  echo
  echo "--- positions ---"
  sqlite3 -header -column "$DB" "select * from positions;"
  echo
  echo "--- open orders ---"
  sqlite3 -header -column "$DB" "select cloid, side, quantity, state, cum_qty from orders where state not in ('FILLED','CANCELLED');"
  sleep 2
done
