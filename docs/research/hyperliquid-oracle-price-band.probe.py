"""Probe for docs/research/hyperliquid-oracle-price-band.md.

Asks Hyperliquid testnet how far from the oracle a BTC order may be priced.
It has two modes.

``passive`` places post-only (ALO) orders of about 12 USD at growing distances
from the oracle. Sells go above it and buys below it, so none of them can fill.
Each accepted order is canceled at once.

``aggressive`` asks whether the venue checks the limit price or the fill price.
It runs only while the book sits within 1% of the oracle. It sends an IOC buy
priced 5% above the oracle, then an IOC sell priced 5% below it. Both cross the
book, so each fills near the oracle while its limit price is 5% away. These are
real fills of about 12 USD. The sell closes the buy.

Both modes end the same way. They cancel any of their own orders still open.
They close any position their own fills left, with a reduce-only order. They
never touch an order or a position they did not create. They refuse to start
if the account has an open order or a BTC position.

This places real testnet orders. It never talks to mainnet. Run it from the
repo root with the testnet key in the environment:

    uv run --env-file .env python <this file> <work dir> [passive|aggressive]

It reads ``TICKWRIGHT_HYPERLIQUID__SIGNING_KEY`` and the optional
``TICKWRIGHT_HYPERLIQUID__ACCOUNT_ADDRESS``. It writes ``results.jsonl`` into
the work dir and prints one line per order.
"""

from __future__ import annotations

import json
import math
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from hyperliquid.utils.types import Cloid

COIN = "BTC"
DISTANCES = [0.01, 0.02, 0.03, 0.05, 0.08]
# The engine's MARKET bound (ADR-0030). A mainnet market buy is priced this far past the touch.
AGGRESSIVE_DISTANCE = 0.05
# Farther than this, the fill price itself is far from the oracle, and the test proves nothing.
BOOK_NEAR_ORACLE = 0.01
NOTIONAL_USD = 12
# Hard-coded so that no environment variable can point this at mainnet.
BASE_URL = constants.TESTNET_API_URL


def snapshot(info: Info) -> dict[str, Any]:
    """The oracle, the mark and the best bid and ask for COIN, read just now."""
    meta, ctxs = info.post("/info", {"type": "metaAndAssetCtxs"})
    idx = next(i for i, u in enumerate(meta["universe"]) if u["name"] == COIN)
    ctx = ctxs[idx]
    bids, asks = info.l2_snapshot(COIN)["levels"]
    return {
        "sz_decimals": meta["universe"][idx]["szDecimals"],
        "oracle": float(ctx["oraclePx"]),
        "mark": float(ctx["markPx"]),
        "bid": float(bids[0]["px"]) if bids else None,
        "ask": float(asks[0]["px"]) if asks else None,
    }


def venue_price(px: float, sz_decimals: int, is_buy: bool) -> float:
    """Round to the venue's price grid, down for a buy and up for a sell."""
    decimals = 6 - sz_decimals
    step = 10**-decimals
    px = math.floor(px / step) * step if is_buy else math.ceil(px / step) * step
    # Five significant figures, rounded the same safe way.
    magnitude = 10 ** (math.floor(math.log10(px)) - 4)
    if magnitude > step:
        px = (
            math.floor(px / magnitude) * magnitude
            if is_buy
            else math.ceil(px / magnitude) * magnitude
        )
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


def is_open(info: Info, address: str, cloid: Cloid) -> bool:
    """True when the venue still lists this order as open."""
    resp = info.query_order_by_cloid(address, cloid)
    return resp.get("status") == "order" and resp["order"]["status"] == "open"


def position(info: Info, address: str) -> float:
    """The account's signed COIN position."""
    for entry in info.user_state(address)["assetPositions"]:
        if entry["position"]["coin"] == COIN:
            return float(entry["position"]["szi"])
    return 0.0


def place_and_cancel(
    exchange: Exchange, info: Info, distance: float, is_buy: bool, cloid: Cloid
) -> dict[str, Any]:
    snap = snapshot(info)
    oracle = snap["oracle"]
    target = oracle * (1 - distance) if is_buy else oracle * (1 + distance)
    px = venue_price(target, snap["sz_decimals"], is_buy)
    sz = size_for(px, snap["sz_decimals"])
    row: dict[str, Any] = {
        "ts_ms": int(time.time() * 1000),
        "cloid": str(cloid),
        "side": "buy" if is_buy else "sell",
        "distance_target": distance,
        "px": px,
        "sz": sz,
        "px_vs_oracle": round(px / oracle - 1, 5),
        "px_vs_mark": round(px / snap["mark"] - 1, 5),
        **snap,
    }
    crosses = (is_buy and snap["ask"] is not None and px >= snap["ask"]) or (
        not is_buy and snap["bid"] is not None and px <= snap["bid"]
    )
    if crosses:
        row["skipped"] = "would cross the book"
        return row
    resp = exchange.order(COIN, is_buy, sz, px, {"limit": {"tif": "Alo"}}, cloid=cloid)
    row["response"] = resp
    row["status"] = first_status(resp)
    resting = row["status"].get("resting") if isinstance(row["status"], dict) else None
    if resting:
        row["cancel"] = exchange.cancel(COIN, resting["oid"])
    return row


def cross_ioc(exchange: Exchange, info: Info, is_buy: bool, cloid: Cloid) -> dict[str, Any]:
    """Send an IOC order priced AGGRESSIVE_DISTANCE past the oracle, into the book."""
    snap = snapshot(info)
    oracle = snap["oracle"]
    sign = 1 if is_buy else -1
    px = venue_price(oracle * (1 + sign * AGGRESSIVE_DISTANCE), snap["sz_decimals"], is_buy)
    touch = snap["ask"] if is_buy else snap["bid"]
    row: dict[str, Any] = {
        "ts_ms": int(time.time() * 1000),
        "cloid": str(cloid),
        "side": "buy" if is_buy else "sell",
        "tif": "Ioc",
        "distance_target": sign * AGGRESSIVE_DISTANCE,
        "px": px,
        "px_vs_oracle": round(px / oracle - 1, 5),
        "touch_vs_oracle": round(touch / oracle - 1, 5) if touch else None,
        **snap,
    }
    if touch is None or abs(touch / oracle - 1) > BOOK_NEAR_ORACLE:
        row["skipped"] = "book is not near the oracle"
        return row
    row["sz"] = size_for(touch, snap["sz_decimals"])
    resp = exchange.order(COIN, is_buy, row["sz"], px, {"limit": {"tif": "Ioc"}}, cloid=cloid)
    row["response"] = resp
    row["status"] = first_status(resp)
    filled = row["status"].get("filled") if isinstance(row["status"], dict) else None
    if filled:
        row["filled_sz"] = sign * float(filled["totalSz"])
        row["fill_vs_oracle"] = round(float(filled["avgPx"]) / oracle - 1, 5)
    return row


def close_probe_position(
    exchange: Exchange, info: Info, net: float, cloid: Cloid
) -> dict[str, Any]:
    """Close what the probe's own fills left, reduce-only, priced 1% through the touch."""
    snap = snapshot(info)
    is_buy = net < 0
    touch = snap["ask"] if is_buy else snap["bid"]
    if touch is None:
        return {"close": "no touch to price against", "net": net}
    px = venue_price(touch * (1.01 if is_buy else 0.99), snap["sz_decimals"], is_buy)
    sz = round(abs(net), snap["sz_decimals"])
    resp = exchange.order(
        COIN, is_buy, sz, px, {"limit": {"tif": "Ioc"}}, reduce_only=True, cloid=cloid
    )
    return {"close": first_status(resp), "net": net, "px": px, "sz": sz, **snap}


def main() -> None:
    work = Path(sys.argv[1])
    mode = sys.argv[2] if len(sys.argv) > 2 else "passive"
    if mode not in ("passive", "aggressive"):
        sys.exit(f"unknown mode {mode!r}, expected passive or aggressive")
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
    print(f"account {address} on {BASE_URL}, mode {mode}")

    sent: list[Cloid] = []
    net = 0.0

    def new_cloid() -> Cloid:
        # Recorded before the order is sent, so cleanup finds it even if the response is lost.
        cloid = Cloid.from_str("0x" + secrets.token_hex(16))
        sent.append(cloid)
        return cloid

    try:
        with (work / "results.jsonl").open("a") as out:
            if mode == "passive":
                steps = [(d, b) for d in DISTANCES for b in (False, True)]
            else:
                steps = [(AGGRESSIVE_DISTANCE, True), (AGGRESSIVE_DISTANCE, False)]
            for distance, is_buy in steps:
                if mode == "passive":
                    row = place_and_cancel(exchange, info, distance, is_buy, new_cloid())
                else:
                    row = cross_ioc(exchange, info, is_buy, new_cloid())
                    net += row.get("filled_sz", 0.0)
                out.write(json.dumps({"mode": mode, **row}) + "\n")
                status = row.get("skipped") or row["status"]
                print(
                    f"{row['side']:4} target {row['distance_target']:+.0%} px {row['px']} "
                    f"vs oracle {row['px_vs_oracle']:+.2%} -> {status}"
                )
                time.sleep(1)
    finally:
        # Look up by cloid, not by the account's open orders. An engine may share the account,
        # and its orders are not ours to cancel. A cloid also finds an order whose place
        # response was lost.
        left = [c for c in sent if is_open(info, address, c)]
        for cloid in left:
            exchange.cancel_by_cloid(COIN, cloid)
        still_open = [c for c in left if is_open(info, address, c)]
        print(f"probe orders open at the end: {len(left)}, still open: {len(still_open)}")
        # Close only the probe's own net fill. The start guard proved the account began flat.
        if round(net, 8):
            closed = close_probe_position(exchange, info, net, new_cloid())
            print(f"closing probe position {net}: {closed['close']}")
            with (work / "results.jsonl").open("a") as out:
                out.write(json.dumps({"mode": mode, **closed}) + "\n")
        left_position = position(info, address)
        print(f"{COIN} position at the end: {left_position}")
        if still_open:
            sys.exit(f"PROBE ORDERS STILL OPEN: {still_open}. Cancel them by hand.")
        if left_position:
            sys.exit(f"{COIN} POSITION LEFT: {left_position}. Close it by hand.")


if __name__ == "__main__":
    main()
