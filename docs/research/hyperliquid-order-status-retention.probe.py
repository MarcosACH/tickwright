"""Probe for docs/research/hyperliquid-order-status-retention.md.

Asks mainnet whether ``orderStatus`` answers ``unknownOid`` for an order whose
fill ``userFills`` still reports. Every read is an unsigned POST to the public
info endpoint. Nothing is placed or signed.

Run it from the repo root with ``uv run python <this file> <work dir>``. It
writes ``addrs.json`` and ``results.jsonl`` into the work dir, then prints the
tables the note quotes. A rerun skips addresses already in ``results.jsonl``.

The note is a dated capture. A rerun will not reproduce its numbers, because
the sample of addresses comes from live ``recentTrades`` and the leaderboard.
"""

from __future__ import annotations

import json
import random
import sys
import time
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any

import requests

API = "https://api.hyperliquid.xyz/info"
LEADERBOARD = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
HLP_VAULT = "0xdfc24b077bc1425ad1dea75bcb6f8158e10df303"
DAY_MS = 86_400_000
# The documented limit is 1200 weight per minute per IP. Stay under it.
WEIGHT_BUDGET_PER_MINUTE = 900
AGE_BUCKETS = [
    (None, 1, "under 1 day"),
    (1, 7, "1 to 7 days"),
    (7, 30, "7 to 30 days"),
    (30, 90, "30 to 90 days"),
    (90, 180, "90 to 180 days"),
    (180, 365, "180 to 365 days"),
    (365, None, "over 365 days"),
]

_session = requests.Session()
_spent: deque[tuple[float, int]] = deque()
_now_ms = int(time.time() * 1000)


def _weight(body: dict[str, Any], rows: int) -> int:
    base = 2 if body["type"] == "orderStatus" else 20
    return base + rows // 20


def post(body: dict[str, Any]) -> Any:
    """One info read, throttled to the weight budget and retried on 429."""
    while sum(w for t, w in _spent if time.time() - t < 60) > WEIGHT_BUDGET_PER_MINUTE:
        time.sleep(1)
    for _ in range(5):
        r = _session.post(API, json=body, timeout=30)
        if r.status_code == 429:
            print("429, sleeping 60s", file=sys.stderr)
            time.sleep(60)
            continue
        r.raise_for_status()
        j = r.json()
        _spent.append((time.time(), _weight(body, len(j) if isinstance(j, list) else 0)))
        time.sleep(0.25)
        return j
    raise RuntimeError(f"gave up on {body['type']}")


def age_days(ms: int) -> float:
    """Days between a venue timestamp and the moment this script started."""
    return round((_now_ms - ms) / DAY_MS, 2)


def collect_addresses() -> dict[str, str]:
    """Addresses to probe, tagged by where each one came from."""
    addrs: dict[str, str] = {}
    meta = post({"type": "meta"})
    coins = [u["name"] for u in meta["universe"] if not u.get("isDelisted")]
    for coin in coins[:6] + coins[6::6][:30]:
        try:
            trades = post({"type": "recentTrades", "coin": coin})
        except Exception as e:  # noqa: BLE001 - a bad coin must not stop the sweep
            print(coin, "err", e, file=sys.stderr)
            continue
        for row in trades:
            for user in row.get("users", []):
                addrs.setdefault(user.lower(), coin)
    try:
        rows = requests.get(LEADERBOARD, timeout=60).json()
        rows = rows.get("leaderboardRows", rows) if isinstance(rows, dict) else rows
        n = len(rows)
        picks = list(range(0, 10)) + list(range(n // 2, n // 2 + 10)) + list(range(n - 30, n, 3))
        for i in picks:
            addrs.setdefault(rows[i]["ethAddress"].lower(), f"lb#{i}")
    except Exception as e:  # noqa: BLE001 - the leaderboard is a bonus source
        print("leaderboard err", e, file=sys.stderr)
    addrs.setdefault(HLP_VAULT, "HLP")
    return addrs


def probe(addr: str, tag: str) -> dict[str, Any]:
    """Ask orderStatus for a spread of this account's filled orders."""
    out: dict[str, Any] = {"addr": addr, "tag": tag}
    f_recent = post({"type": "userFills", "user": addr})
    f_old = post({"type": "userFillsByTime", "user": addr, "startTime": 0})
    hist = post({"type": "historicalOrders", "user": addr})
    opn = post({"type": "openOrders", "user": addr})

    fills: dict[int, int] = {}
    fill_meta: dict[int, dict[str, Any]] = {}
    for x in f_recent + f_old:
        fills[x["oid"]] = min(fills.get(x["oid"], x["time"]), x["time"])
        fill_meta.setdefault(
            x["oid"],
            {"dir": x["dir"], "hash": x["hash"], "coin": x["coin"], "cloid": x.get("cloid")},
        )
    all_t = [x["time"] for x in f_recent + f_old]
    out["n_fills_recent"] = len(f_recent)
    out["n_fills_old"] = len(f_old)
    out["fill_oldest_days"] = age_days(min(all_t)) if all_t else None
    out["fill_newest_days"] = age_days(max(all_t)) if all_t else None
    out["fill_has_cloid_key"] = any("cloid" in x for x in f_recent[:50])

    hist_by_oid = {o["order"]["oid"]: o for o in hist}
    ht = [o["order"]["timestamp"] for o in hist]
    out["n_hist"] = len(hist)
    out["hist_oldest_days"] = age_days(min(ht)) if ht else None
    out["hist_newest_days"] = age_days(max(ht)) if ht else None
    out["hist_with_cloid"] = sum(1 for o in hist if o["order"].get("cloid"))
    open_oids = {o["oid"] for o in opn}
    out["n_open"] = len(opn)

    # Sample by fill age: the 2 oldest, the 2 newest, 12 spread evenly, and 6
    # around the oldest historicalOrders row.
    items = sorted(fills.items(), key=lambda kv: kv[1])
    picks: list[tuple[int, int]] = []
    if items:
        picks += items[:2] + items[-2:]
        picks += items[:: max(1, len(items) // 12)]
        if ht:
            b = min(ht)
            picks += [kv for kv in items if kv[1] < b][-3:]
            picks += [kv for kv in items if kv[1] >= b][:3]
    seen: set[int] = set()
    rows = []
    for oid, t in picks:
        if oid in seen:
            continue
        seen.add(oid)
        r = post({"type": "orderStatus", "user": addr, "oid": oid})
        rows.append(
            {
                "oid": oid,
                "age_days": age_days(t),
                "in_hist": oid in hist_by_oid,
                "in_open": oid in open_oids,
                "answer": r["status"],
                "order_status": r.get("order", {}).get("status"),
                **fill_meta[oid],
            }
        )
    out["oid_probes"] = rows

    # Cloid versus oid, on historicalOrders rows that carry a cloid.
    with_cloid = [o for o in hist if o["order"].get("cloid")]
    random.shuffle(with_cloid)
    cl = []
    for o in with_cloid[:4]:
        oid, cloid = o["order"]["oid"], o["order"]["cloid"]
        by_oid = post({"type": "orderStatus", "user": addr, "oid": oid})
        by_cl = post({"type": "orderStatus", "user": addr, "oid": cloid})
        cl.append(
            {
                "oid": oid,
                "cloid": cloid,
                "age_days": age_days(o["order"]["timestamp"]),
                "hist_status": o["status"],
                "by_oid": by_oid["status"],
                "by_cloid": by_cl["status"],
                "same_oid": by_cl.get("order", {}).get("order", {}).get("oid") == oid,
                "fill_retained": oid in fills,
            }
        )
    out["cloid_probes"] = cl

    # Orders that lost their record, asked again by the cloid on their fill row.
    purged = []
    for p in rows:
        if p["answer"] != "unknownOid" or not p["cloid"]:
            continue
        by_cl = post({"type": "orderStatus", "user": addr, "oid": p["cloid"]})
        purged.append({"oid": p["oid"], "cloid": p["cloid"], "by_cloid": by_cl["status"]})
        if len(purged) >= 6:
            break
    out["purged_cloid_probes"] = purged
    return out


def is_system_fill(p: dict[str, Any]) -> bool:
    """A userFills row with no user order behind it."""
    d = p.get("dir", "")
    return d == "Spot Dust Conversion" or d.startswith("Liquidat")


def in_bucket(age: float, lo: float | None, hi: float | None) -> bool:
    # A fill that landed after the script stamped its clock reads slightly
    # negative. It belongs in the youngest bucket, so the low edge is open.
    return (lo is None or age >= lo) and (hi is None or age < hi)


def report(results: list[dict[str, Any]]) -> None:
    """Print the tables the research note quotes."""
    res = [r for r in results if r["oid_probes"]]
    print("accounts with fills:", len(res))
    tab: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    total = 0
    system = 0
    present_but_unknown = 0
    record_outside_window = 0
    unknown: list[tuple[float, str, int]] = []
    oldest_record: tuple[float, str, int] | None = None
    for r in res:
        for p in r["oid_probes"]:
            if is_system_fill(p):
                system += 1
                continue
            total += 1
            a = p["age_days"]
            has_record = p["answer"] == "order"
            for lo, hi, name in AGE_BUCKETS:
                if in_bucket(a, lo, hi):
                    tab[name][0] += 1
                    tab[name][1 if has_record else 2] += 1
            present = p["in_hist"] or p["in_open"]
            if present and not has_record:
                present_but_unknown += 1
            if has_record and not present:
                record_outside_window += 1
            if has_record and (oldest_record is None or a > oldest_record[0]):
                oldest_record = (a, r["addr"], p["oid"])
            if p["answer"] == "unknownOid":
                unknown.append((a, r["addr"], p["oid"]))
    print("user-order probes:", total, "system fills excluded:", system)
    print("| Fill age | Probed | Answered with a record | Answered unknownOid |")
    print("| --- | --- | --- | --- |")
    for _, _, name in AGE_BUCKETS:
        n, o, u = tab[name]
        print(f"| {name} | {n} | {o} | {u} |")
    print(f"| total | {total} | {total - len(unknown)} | {len(unknown)} |")
    print("present in historicalOrders or openOrders but unknownOid:", present_but_unknown)
    print("record but absent from both:", record_outside_window)
    print("oldest answered with a record:", oldest_record)
    print("youngest unknownOid on a retained fill:", min(unknown, default=None))
    print("oldest unknownOid on a retained fill:", max(unknown, default=None))
    print("unknownOid rows:", len(unknown), "accounts:", len({a for _, a, _ in unknown}))

    cl = [p for r in res for p in r["cloid_probes"]]
    both = sum(1 for p in cl if p["by_oid"] == "order" and p["by_cloid"] == "order")
    print("cloid probes:", len(cl), "record by both keys:", both)
    print("same oid in body:", sum(1 for p in cl if p["same_oid"]))
    print("keys disagree:", sum(1 for p in cl if p["by_oid"] != p["by_cloid"]))
    purged = [p for r in res for p in r.get("purged_cloid_probes", [])]
    still_unknown = sum(1 for p in purged if p["by_cloid"] == "unknownOid")
    print("purged orders asked by cloid:", len(purged), "still unknownOid:", still_unknown)
    print("accounts whose recent fills carry cloid:", Counter(r["fill_has_cloid_key"] for r in res))


def main(work: Path) -> None:
    work.mkdir(parents=True, exist_ok=True)
    addrs_path = work / "addrs.json"
    results_path = work / "results.jsonl"
    if addrs_path.exists():
        addrs = json.loads(addrs_path.read_text())
    else:
        addrs = collect_addresses()
        addrs_path.write_text(json.dumps(addrs, indent=0))

    random.seed(242)
    lb = [(a, t) for a, t in addrs.items() if t.startswith("lb#")]
    rt = [(a, t) for a, t in addrs.items() if not t.startswith("lb#") and t != "HLP"]
    random.shuffle(rt)
    chosen = [(HLP_VAULT, "HLP")] + lb + rt[:45]

    done: set[str] = set()
    if results_path.exists():
        done = {json.loads(line)["addr"] for line in results_path.read_text().splitlines()}
    with results_path.open("a") as fh:
        for i, (a, t) in enumerate(chosen):
            if a in done:
                continue
            try:
                res = probe(a, t)
            except Exception as e:  # noqa: BLE001 - one bad account must not stop the sweep
                print(i, a, "ERR", e, file=sys.stderr)
                time.sleep(5)
                continue
            fh.write(json.dumps(res) + "\n")
            fh.flush()
            unk = sum(1 for p in res["oid_probes"] if p["answer"] == "unknownOid")
            print(i, t, a[:10], "probes", len(res["oid_probes"]), "unknownOid", unk, flush=True)

    report([json.loads(line) for line in results_path.read_text().splitlines()])


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/hl-order-status-retention"))
