"""Probe for issue #652: two resting orders under one cloid.

Asks Hyperliquid testnet two questions:

1. Does the venue accept a place under a cloid whose earlier order still rests?
2. If it does, which order does ``orderStatus`` by that cloid return?
3. If it does, which order does a cancel by that cloid remove?

It places two post-only BTC buys of about 12 USD under one fresh cloid. The
first sits 5% below the oracle and the second 6% below it. Neither can fill,
and the two prices tell them apart. In every case it reads
``frontendOpenOrders`` once. If both rest, it reads ``orderStatus`` by the
cloid several times, then by each oid. Then it cancels once by the cloid and
lists which orders are still open.

It always ends by canceling any of its own orders still open, by oid. It never
touches an order it did not create. It refuses to start if the account has an
open order or a BTC position.

This places real testnet orders. It never talks to mainnet. Run it from the
repo root with the testnet key in the environment:

    uv run --env-file .env python <this file> <work dir>

It reads ``TICKWRIGHT_HYPERLIQUID__SIGNING_KEY`` and the optional
``TICKWRIGHT_HYPERLIQUID__ACCOUNT_ADDRESS``. It writes ``results.jsonl`` into
the work dir and prints the two answers.
"""

from __future__ import annotations

import json
import math
import os
import secrets
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from hyperliquid.utils.types import Cloid

COIN = "BTC"
# The oracle-price-band probe saw passive orders rest up to 8% from the oracle.
FIRST_DISTANCE = 0.05
SECOND_DISTANCE = 0.06
NOTIONAL_USD = 12
# Several reads, because an undocumented choice between two orders may not be stable.
CLOID_READS = 5
# Hard-coded so that no environment variable can point this at mainnet.
BASE_URL = constants.TESTNET_API_URL


def snapshot(info: Info) -> dict[str, Any]:
    """The oracle, the mark and the best bid for COIN, read just now."""
    meta, ctxs = info.post("/info", {"type": "metaAndAssetCtxs"})
    idx = next(i for i, u in enumerate(meta["universe"]) if u["name"] == COIN)
    bids, _ = info.l2_snapshot(COIN)["levels"]
    return {
        "sz_decimals": meta["universe"][idx]["szDecimals"],
        "oracle": float(ctxs[idx]["oraclePx"]),
        "mark": float(ctxs[idx]["markPx"]),
        "bid": float(bids[0]["px"]) if bids else None,
    }


def buy_price(px: float, sz_decimals: int) -> float:
    """Round a buy price down onto the venue's grid, so it moves away from the book."""
    decimals = 6 - sz_decimals
    step = 10**-decimals
    px = math.floor(px / step) * step
    # The venue also allows at most five significant figures.
    magnitude = 10 ** (math.floor(math.log10(px)) - 4)
    if magnitude > step:
        px = math.floor(px / magnitude) * magnitude
    return round(px, decimals)


def size_for(px: float, sz_decimals: int) -> float:
    """The smallest size on the venue's grid worth at least NOTIONAL_USD at px."""
    step = 10**-sz_decimals
    return round(math.ceil(NOTIONAL_USD / px / step) * step, sz_decimals)


def first_status(resp: dict[str, Any]) -> Any:
    """The order's status, or the whole response when the batch failed."""
    # A batch-level error puts a plain string in "response", with no statuses.
    body = resp.get("response")
    statuses = body.get("data", {}).get("statuses", []) if isinstance(body, dict) else []
    return statuses[0] if statuses else resp


def place_buy(exchange: Exchange, info: Info, distance: float, cloid: Cloid) -> dict[str, Any]:
    """Place a post-only buy this far below the oracle. The row holds the oid if it rests."""
    snap = snapshot(info)
    px = buy_price(snap["oracle"] * (1 - distance), snap["sz_decimals"])
    sz = size_for(px, snap["sz_decimals"])
    resp = exchange.order(COIN, True, sz, px, {"limit": {"tif": "Alo"}}, cloid=cloid)
    status = first_status(resp)
    resting = status.get("resting") if isinstance(status, dict) else None
    return {
        "ts_ms": int(time.time() * 1000),
        "distance": distance,
        "px": px,
        "sz": sz,
        **snap,
        "response": resp,
        "status": status,
        "oid": resting["oid"] if resting else None,
    }


def listed_under(info: Info, address: str, cloid: Cloid) -> list[dict[str, Any]]:
    """The account's open orders that carry this cloid. Only the probe used it."""
    rows = info.post("/info", {"type": "frontendOpenOrders", "user": address})
    want = cloid.to_raw().lower()
    return [o for o in rows if (o.get("cloid") or "").lower() == want]


def position(info: Info, address: str) -> float:
    """The account's signed COIN position."""
    for entry in info.user_state(address)["assetPositions"]:
        if entry["position"]["coin"] == COIN:
            return float(entry["position"]["szi"])
    return 0.0


def read_status(resp: dict[str, Any]) -> dict[str, Any]:
    """The parts of an orderStatus answer the note needs."""
    record = resp.get("order", {})
    return {
        "answer": resp.get("status"),
        "oid": record.get("order", {}).get("oid"),
        "order_status": record.get("status"),
    }


def main() -> None:
    work = Path(sys.argv[1])
    work.mkdir(parents=True, exist_ok=True)
    key = os.environ["TICKWRIGHT_HYPERLIQUID__SIGNING_KEY"]
    wallet = Account.from_key(key)
    address = os.environ.get("TICKWRIGHT_HYPERLIQUID__ACCOUNT_ADDRESS") or wallet.address
    info = Info(BASE_URL, skip_ws=True)
    exchange = Exchange(wallet, BASE_URL, account_address=address)

    if info.open_orders(address):
        sys.exit(f"{address} already has open orders. Refusing to start.")
    if position(info, address):
        sys.exit(f"{address} already has a {COIN} position. Refusing to start.")

    cloid = Cloid.from_str("0x" + secrets.token_hex(16))
    print(f"account {address} on {BASE_URL}, cloid {cloid.to_raw()}")
    oids: list[int] = []
    out = (work / "results.jsonl").open("a")

    def record(step: str, row: dict[str, Any]) -> None:
        out.write(json.dumps({"step": step, "cloid": cloid.to_raw(), **row}) + "\n")
        out.flush()

    try:
        first = place_buy(exchange, info, FIRST_DISTANCE, cloid)
        record("place first", first)
        print(f"first place  px {first['px']} -> {first['status']}")
        if first["oid"] is None:
            sys.exit("The first order did not rest, so the probe cannot ask its question.")
        oids.append(first["oid"])
        time.sleep(1)

        second = place_buy(exchange, info, SECOND_DISTANCE, cloid)
        record("place second", second)
        print(f"second place px {second['px']} -> {second['status']}")
        if second["oid"] is not None:
            oids.append(second["oid"])
        time.sleep(1)

        listed = listed_under(info, address, cloid)
        record("frontendOpenOrders", {"orders": listed})
        print(f"open under the cloid: oids {[o['oid'] for o in listed]}")

        print()
        print("Q1. Accepts a place under a cloid whose earlier order rests:", end=" ")
        print("yes" if second["oid"] is not None else "no")
        if second["oid"] is None:
            return

        names = {first["oid"]: "first", second["oid"]: "second"}
        picked: Counter[str] = Counter()
        for i in range(CLOID_READS):
            read = read_status(info.query_order_by_cloid(address, cloid))
            record("orderStatus by cloid", {"read": i, **read})
            picked[names.get(read["oid"], f"other: {read}")] += 1
            time.sleep(1)
        for oid in oids:
            read = read_status(info.query_order_by_oid(address, oid))
            record("orderStatus by oid", {"asked": oid, **read})
            print(f"by oid {oid} ({names[oid]}): {read}")
        print(f"Q2. orderStatus by the cloid returned, over {CLOID_READS} reads: {dict(picked)}")

        resp = exchange.cancel_by_cloid(COIN, cloid)
        record("cancel by cloid", {"response": resp})
        print(f"cancel by cloid -> {first_status(resp)}")
        time.sleep(1)
        left = {o["oid"] for o in listed_under(info, address, cloid)}
        record("frontendOpenOrders after cancel by cloid", {"oids": sorted(left)})
        removed = [names[oid] for oid in oids if oid not in left]
        print(f"Q3. A cancel by the cloid removed: {removed or 'neither'}")
    finally:
        # Cancel by oid. Add any order the cloid lists, in case a place answer was lost.
        # The cloid is fresh and random, so every order under it is the probe's own.
        targets = set(oids) | {o["oid"] for o in listed_under(info, address, cloid)}
        for oid in sorted(targets):
            record("cancel", {"oid": oid, "response": exchange.cancel(COIN, oid)})
        still_open = [o["oid"] for o in listed_under(info, address, cloid)]
        left_position = position(info, address)
        out.close()
        print()
        # An oid the cancel by cloid already removed answers with an error here. That is harmless.
        print(f"cleanup cancel requests: {len(targets)}, still open: {len(still_open)}")
        print(f"{COIN} position at the end: {left_position}")
        if still_open:
            sys.exit(f"PROBE ORDERS STILL OPEN: {still_open}. Cancel them by hand.")
        if left_position:
            sys.exit(f"{COIN} POSITION LEFT: {left_position}. Close it by hand.")


if __name__ == "__main__":
    main()
