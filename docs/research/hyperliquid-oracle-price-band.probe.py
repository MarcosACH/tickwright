"""Probe for docs/research/hyperliquid-oracle-price-band.md.

Asks Hyperliquid testnet how far from the oracle a resting BTC order may sit.
It places post-only (ALO) orders of about 12 USD at growing distances from the
oracle. Sells go above it and buys below it, so none of them can fill. Each
accepted order is canceled at once. The run ends by checking that none of its
own orders is left open. It never cancels an order it did not place.

This places real testnet orders. It never talks to mainnet. Run it from the
repo root with the testnet key in the environment:

    uv run --env-file .env python <this file> <work dir>

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
    """Round to the venue's price grid, away from the book so ALO cannot cross."""
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


def is_open(info: Info, address: str, cloid: Cloid) -> bool:
    """True when the venue still lists this order as open."""
    resp = info.query_order_by_cloid(address, cloid)
    return resp.get("status") == "order" and resp["order"]["status"] == "open"


def place_and_cancel(
    exchange: Exchange, info: Info, distance: float, is_buy: bool, cloid: Cloid
) -> dict[str, Any]:
    snap = snapshot(info)
    oracle = snap["oracle"]
    target = oracle * (1 - distance) if is_buy else oracle * (1 + distance)
    px = venue_price(target, snap["sz_decimals"], is_buy)
    step = 10 ** -snap["sz_decimals"]
    sz = round(math.ceil(NOTIONAL_USD / px / step) * step, snap["sz_decimals"])
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
    # A batch-level error puts a plain string in "response", with no statuses.
    body = resp.get("response")
    statuses = body.get("data", {}).get("statuses", []) if isinstance(body, dict) else []
    row["status"] = statuses[0] if statuses else resp
    resting = statuses[0].get("resting") if statuses else None
    if resting:
        row["cancel"] = exchange.cancel(COIN, resting["oid"])
    return row


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
    print(f"account {address} on {BASE_URL}")

    sent: list[Cloid] = []
    try:
        with (work / "results.jsonl").open("a") as out:
            for distance in DISTANCES:
                for is_buy in (False, True):
                    cloid = Cloid.from_str("0x" + secrets.token_hex(16))
                    sent.append(cloid)
                    row = place_and_cancel(exchange, info, distance, is_buy, cloid)
                    out.write(json.dumps(row) + "\n")
                    status = row.get("skipped") or row["status"]
                    print(
                        f"{row['side']:4} target {distance:.0%} px {row['px']} "
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
        if still_open:
            sys.exit(f"PROBE ORDERS STILL OPEN: {still_open}. Cancel them by hand.")


if __name__ == "__main__":
    main()
