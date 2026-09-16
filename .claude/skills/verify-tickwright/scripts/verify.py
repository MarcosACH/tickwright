#!/usr/bin/env python3
"""The harness ``/verify-tickwright`` drives the real ``tickwright`` process through.

One run lives at ``.agents/verify/<run-id>/``. ``scratch/`` holds the instance
(its ``.env``, tick files, SQLite store) and is deleted at cleanup. ``evidence/``
holds the proof (stderr logs, exit codes, store dumps, signal log) and is kept.

Every engine life is spawned with ``TICKWRIGHT_*`` scrubbed from the inherited
environment, so the operator's own ``.env`` and exported variables can never
reach an instance. The one exception is ``start --forward-key``, which copies the
two Hyperliquid credential variables from the repo ``.env`` into the child's
environment only. They never land in a scratch file or in evidence, and
``cleanup`` scans the evidence for the key before it finishes.

Run it through the wrapper beside it: ``.claude/skills/verify-tickwright/scripts/verify``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parents[3]
# ``VERIFY_HOME`` is for the helper's own tests, which must never write into the
# checkout's run tree. Nothing else sets it.
VERIFY_HOME = Path(os.environ.get("VERIFY_HOME") or ROOT / ".agents" / "verify")
ENGINE_CMD = [sys.executable, "-m", "tickwright.app"]
STRATEGY_RUNNER = SCRIPTS / "run_strategy.py"
KEY_VARS = ("TICKWRIGHT_HYPERLIQUID__SIGNING_KEY", "TICKWRIGHT_HYPERLIQUID__ACCOUNT_ADDRESS")
SIGNALS = {
    "TERM": signal.SIGTERM,
    "INT": signal.SIGINT,
    "KILL": signal.SIGKILL,
    "USR1": signal.SIGUSR1,
    "USR2": signal.SIGUSR2,
}

# The instrument specs every paper preset carries. Fees and funding stay at the
# frictionless default here. A feature that wants them passes its own
# ``--env TICKWRIGHT_PAPER__INSTRUMENT_SPECS=...`` over the preset.
PAPER_SPECS = {
    symbol: {
        "symbol": symbol,
        "sz_decimals": 3,
        "max_decimals": 6,
        "max_sig_figs": 5,
        "min_notional": "10",
        "max_leverage": 20,
        "margin_maint": "0.02",
    }
    for symbol in ("BTC", "ETH")
}

PRESETS: dict[str, dict[str, str]] = {
    # The hermetic default path: replay file, paper venue, SQLite, in-memory bus.
    "paper-replay": {
        "TICKWRIGHT_REPLAY__PATH": "ticks.jsonl",
        "TICKWRIGHT_SQLITE__PATH": "store.db",
        "TICKWRIGHT_PAPER__GENESIS_COLLATERAL": "100000",
        "TICKWRIGHT_PAPER__INSTRUMENT_SPECS": json.dumps(PAPER_SPECS),
        "TICKWRIGHT_STRATEGIES": "[]",
    },
    # The public mainnet trade stream into the paper venue. No key, no funds.
    "paper-livefeed": {
        "TICKWRIGHT_FEED": "hyperliquid",
        "TICKWRIGHT_HYPERLIQUID__SYMBOLS": '["BTC"]',
        "TICKWRIGHT_HYPERLIQUID__TESTNET": "false",
        "TICKWRIGHT_SQLITE__PATH": "store.db",
        "TICKWRIGHT_PAPER__GENESIS_COLLATERAL": "100000",
        "TICKWRIGHT_PAPER__INSTRUMENT_SPECS": json.dumps(PAPER_SPECS),
        "TICKWRIGHT_STRATEGIES": "[]",
    },
    # Testnet feed and testnet exchange. The key is forwarded, never written.
    "testnet": {
        "TICKWRIGHT_FEED": "hyperliquid",
        "TICKWRIGHT_EXCHANGE": "hyperliquid",
        "TICKWRIGHT_HYPERLIQUID__SYMBOLS": '["BTC"]',
        "TICKWRIGHT_HYPERLIQUID__TESTNET": "true",
        "TICKWRIGHT_SQLITE__PATH": "store.db",
        "TICKWRIGHT_STRATEGIES": "[]",
    },
}


# --- paths and small helpers -------------------------------------------------


def run_dir(run_id: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", run_id):
        sys.exit(f"run-id must be lowercase [a-z0-9_-], got {run_id!r}")
    return VERIFY_HOME / run_id


def scratch(run_id: str) -> Path:
    return run_dir(run_id) / "scratch"


def evidence(run_id: str) -> Path:
    return run_dir(run_id) / "evidence"


def require_run(run_id: str) -> None:
    if not evidence(run_id).is_dir():
        sys.exit(f"no run {run_id}: run `verify init {run_id}` first")


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    ).stdout.strip()


def read_run_json(run_id: str) -> dict:
    path = evidence(run_id) / "run.json"
    return json.loads(path.read_text()) if path.exists() else {}


def write_run_json(run_id: str, data: dict) -> None:
    (evidence(run_id) / "run.json").write_text(json.dumps(data, indent=2) + "\n")


def note(run_id: str, life: str, kind: str, text: str) -> None:
    """Append one timestamped line to a life's signal log."""
    with (evidence(run_id) / f"{life}.signals.txt").open("a") as sink:
        sink.write(f"{now_iso()} {kind} {text}\n")


def scrubbed_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith("TICKWRIGHT_")}


def repo_env_values(names: tuple[str, ...]) -> dict[str, str]:
    """The named variables from the repo ``.env``, values never printed."""
    path = ROOT / ".env"
    found: dict[str, str] = {}
    if not path.exists():
        return found
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() in names:
            found[key.strip()] = value.strip().strip("\"'")
    return found


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def pid_is_ours(pid: int, run_id: str) -> bool:
    """Only ever signal a process whose command line names this run's scratch dir
    or the engine module. Never kill by name."""
    out = subprocess.run(
        ["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True
    ).stdout
    return "tickwright.app" in out or "run_strategy.py" in out or str(scratch(run_id)) in out


def parse_offset(text: str) -> int:
    """``0``, ``500ms``, ``30s``, ``2m``, ``1h`` to nanoseconds."""
    match = re.fullmatch(r"(\d+)(ms|s|m|h)?", text)
    if match is None:
        sys.exit(f"bad offset {text!r}: use 0, 500ms, 30s, 2m or 1h")
    scale = {None: 1_000_000_000, "ms": 1_000_000, "s": 1_000_000_000}
    scale["m"] = 60 * scale["s"]
    scale["h"] = 60 * scale["m"]
    return int(match.group(1)) * scale[match.group(2)]


# --- store access ---------------------------------------------------------------


def env_file_values(run_id: str) -> dict[str, str]:
    path = scratch(run_id) / ".env"
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                values[key] = value
    return values


def store_query(run_id: str, sql: str) -> list[tuple]:
    """Run one read-only query against the run's store, SQLite or Postgres."""
    values = env_file_values(run_id)
    if values.get("TICKWRIGHT_STORE", "sqlite") == "postgres":
        import psycopg

        dsn = values.get(
            "TICKWRIGHT_POSTGRES__DSN",
            "postgresql://tickwright:tickwright@localhost:5432/tickwright",
        )
        # A table the store has not created yet is "no rows yet", the same
        # answer the SQLite branch gives for a file that does not exist yet.
        try:
            with psycopg.connect(dsn) as conn:
                return list(conn.execute(sql).fetchall())
        except psycopg.errors.UndefinedTable:
            return []
    db = scratch(run_id) / values.get("TICKWRIGHT_SQLITE__PATH", "store.db")
    if not db.exists():
        return []
    # ``closing``, not the bare ``with``: a sqlite3 connection's context manager
    # commits, it does not close, and an open handle is a ResourceWarning.
    try:
        with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as conn:
            return list(conn.execute(sql).fetchall())
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []
        raise


STORE_TABLES = ("account", "positions", "orders", "kill_switch", "funding_marks")


def store_dump(run_id: str) -> str:
    lines: list[str] = []
    for table in STORE_TABLES:
        lines.append(f"--- {table}")
        try:
            columns = (
                "cloid, strategy_id, symbol, side, quantity, order_type, state, cum_qty, reason"
            )
            sql = f"select {columns} from orders" if table == "orders" else f"select * from {table}"
            rows = store_query(run_id, sql)
        except Exception as exc:  # noqa: BLE001 - a dump reports, it never raises
            lines.append(f"(unreadable: {exc!r})")
            continue
        if table == "orders":
            lines.append(columns)
        lines.extend(" | ".join(str(v) for v in row) for row in rows)
        if not rows:
            lines.append("(empty)")
    return "\n".join(lines) + "\n"


# --- subcommands -----------------------------------------------------------------


def cmd_doctor(_: argparse.Namespace) -> int:
    """Read-only. Answers: is this checkout worth driving?"""
    ok = True
    print(f"repo      {ROOT}")
    print(
        f"branch    {git('rev-parse', '--abbrev-ref', 'HEAD')} @ {git('rev-parse', '--short', 'HEAD')}"
    )
    dirty = git("status", "--porcelain")
    print(f"tree      {'dirty' if dirty else 'clean'}")
    print(f"python    {sys.executable} ({sys.version.split()[0]})")
    try:
        from importlib.metadata import version

        print(f"package   tickwright {version('tickwright')} importable")
    except Exception as exc:  # noqa: BLE001
        print(f"package   NOT importable: {exc!r}  -> run `uv sync`")
        ok = False
    print(f"sqlite3   {shutil.which('sqlite3') or 'missing (the helper uses the stdlib module)'}")
    docker = subprocess.run(["docker", "info"], capture_output=True, text=True)
    print(
        f"docker    {'daemon up' if docker.returncode == 0 else 'daemon down (kafka and postgres paths unavailable)'}"
    )
    keys = repo_env_values(KEY_VARS)
    print(
        f"repo .env {'has a signing key (testnet path available)' if KEY_VARS[0] in keys else 'no signing key (testnet path unavailable)'}"
    )
    live: list[str] = []
    if VERIFY_HOME.exists():
        for pid_file in VERIFY_HOME.glob("*/scratch/*.pid"):
            pid = int(pid_file.read_text().strip() or 0)
            if pid and pid_alive(pid):
                live.append(f"{pid_file.parent.parent.name}/{pid_file.stem} pid {pid}")
    print(f"instances {', '.join(live) if live else 'none running'}")
    return 0 if ok else 1


def cmd_init(args: argparse.Namespace) -> int:
    path = run_dir(args.run_id)
    if path.exists():
        sys.exit(f"{path} already exists: pick a new run-id, or `verify cleanup {args.run_id}`")
    scratch(args.run_id).mkdir(parents=True)
    evidence(args.run_id).mkdir()
    write_run_json(
        args.run_id,
        {
            "run_id": args.run_id,
            "started_at": now_iso(),
            "sha": git("rev-parse", "HEAD"),
            "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "feature": args.feature,
            "infra": [],
        },
    )
    print(f"scratch   {scratch(args.run_id)}")
    print(f"evidence  {evidence(args.run_id)}")
    return 0


def cmd_ticks(args: argparse.Namespace) -> int:
    """Write a replay tick file. Rows are ``SYMBOL:PRICE[:SIZE[:SIDE]]@OFFSET``."""
    require_run(args.run_id)
    base = datetime.fromisoformat(args.base).astimezone(UTC)
    base_ns = int(base.timestamp() * 1_000_000_000)
    rows: list[str] = []
    for index, spec in enumerate(args.row, start=1):
        head, _, offset = spec.partition("@")
        parts = head.split(":")
        if len(parts) < 2 or not offset:
            sys.exit(f"bad row {spec!r}: use SYMBOL:PRICE[:SIZE[:SIDE]]@OFFSET")
        symbol, price = parts[0], parts[1]
        size = parts[2] if len(parts) > 2 else "1"
        side = parts[3] if len(parts) > 3 else ("buy" if index % 2 else "sell")
        rows.append(
            json.dumps(
                {
                    "symbol": symbol,
                    "price": price,
                    "size": size,
                    "aggressor_side": side,
                    "trade_id": f"{args.name}-{index}",
                    "ts_event": base_ns + parse_offset(offset),
                }
            )
        )
    text = "\n".join(rows) + "\n"
    (scratch(args.run_id) / f"{args.name}.jsonl").write_text(text)
    (evidence(args.run_id) / f"{args.name}.jsonl").write_text(text)
    print(f"wrote {len(rows)} rows to scratch/{args.name}.jsonl (copy in evidence/)")
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    require_run(args.run_id)
    env_values: dict[str, str] = dict(PRESETS[args.preset]) if args.preset else {}
    for pair in args.env:
        key, _, value = pair.partition("=")
        if "SIGNING_KEY" in key:
            sys.exit("never pass the signing key through --env: use --forward-key")
        env_values[key] = value
    env_text = "".join(f"{k}={v}\n" for k, v in env_values.items())
    (scratch(args.run_id) / ".env").write_text(env_text)
    (evidence(args.run_id) / f"{args.life}.env.txt").write_text(env_text)

    child_env = scrubbed_env()
    if args.forward_key:
        keys = repo_env_values(KEY_VARS)
        if KEY_VARS[0] not in keys:
            sys.exit("no TICKWRIGHT_HYPERLIQUID__SIGNING_KEY in the repo .env")
        child_env.update(keys)
    for pair in args.param:
        key, _, value = pair.partition("=")
        child_env[f"VERIFY_{key}"] = value

    if args.strategy:
        command = [sys.executable, str(STRATEGY_RUNNER), args.strategy]
    else:
        command = list(ENGINE_CMD)

    pid_file = scratch(args.run_id) / f"{args.life}.pid"
    exit_file = evidence(args.run_id) / f"{args.life}.exit"
    if pid_file.exists() or exit_file.exists():
        sys.exit(f"life {args.life} already exists in run {args.run_id}: pick another name")

    supervisor = [
        sys.executable,
        str(Path(__file__).resolve()),
        "_supervise",
        args.run_id,
        args.life,
        "--",
        *command,
    ]
    # Detached: the supervisor outlives this helper call and writes the exit code.
    subprocess.Popen(
        supervisor,
        cwd=scratch(args.run_id),
        env=child_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not pid_file.exists():
        time.sleep(0.05)
    if not pid_file.exists():
        sys.exit("the supervisor never reported a pid")
    pid = pid_file.read_text().strip()
    note(args.run_id, args.life, "start", f"pid {pid} {' '.join(command)}")
    print(f"life      {args.life} pid {pid}")
    print(f"stderr    {evidence(args.run_id) / (args.life + '.stderr.jsonl')}")
    print(f"exit code {exit_file} (written when the process ends)")
    return 0


def cmd_supervise(args: argparse.Namespace) -> int:
    """Internal. Spawn the engine, record its pid, wait, record its exit code."""
    stderr_path = evidence(args.run_id) / f"{args.life}.stderr.jsonl"
    stdout_path = evidence(args.run_id) / f"{args.life}.stdout.txt"
    with stderr_path.open("wb") as err, stdout_path.open("wb") as out:
        child = subprocess.Popen(args.command, cwd=scratch(args.run_id), stdout=out, stderr=err)
        (scratch(args.run_id) / f"{args.life}.pid").write_text(str(child.pid))
        code = child.wait()
    (evidence(args.run_id) / f"{args.life}.exit").write_text(f"{code}\n")
    note(args.run_id, args.life, "exit", f"code {code}")
    return 0


def cmd_await(args: argparse.Namespace) -> int:
    require_run(args.run_id)
    log = evidence(args.run_id) / f"{args.life}.stderr.jsonl"
    exit_file = evidence(args.run_id) / f"{args.life}.exit"
    deadline = time.monotonic() + args.timeout
    while True:
        if args.event:
            text = log.read_text() if log.exists() else ""
            hits = [line for line in text.splitlines() if f'"event": "{args.event}"' in line]
            if len(hits) >= args.count:
                print(hits[args.count - 1])
                return 0
        elif args.order:
            cloid, _, state = args.order.partition("=")
            rows = store_query(args.run_id, f"select state from orders where cloid = '{cloid}'")
            # The store writes the state lowercase; the operator reads it as the enum name.
            if rows and str(rows[0][0]).lower() == state.lower():
                print(f"{cloid} is {state}")
                return 0
        elif args.sql:
            rows = store_query(args.run_id, args.sql)
            got = str(rows[0][0]) if rows else ""
            if got == args.expect:
                print(f"query returned {got!r}")
                return 0
        elif args.exit:
            if exit_file.exists():
                print(f"exit code {exit_file.read_text().strip()}")
                return 0
        if time.monotonic() > deadline:
            break
        time.sleep(0.1)
    tail = "\n".join(log.read_text().splitlines()[-5:]) if log.exists() else "(no log yet)"
    print(f"TIMEOUT after {args.timeout}s. Last log lines:\n{tail}", file=sys.stderr)
    return 1


def cmd_signal(args: argparse.Namespace) -> int:
    require_run(args.run_id)
    pid_file = scratch(args.run_id) / f"{args.life}.pid"
    if not pid_file.exists():
        sys.exit(f"no pid for life {args.life}")
    pid = int(pid_file.read_text().strip())
    if not pid_alive(pid):
        sys.exit(f"pid {pid} is not running")
    if not pid_is_ours(pid, args.run_id):
        sys.exit(f"pid {pid} is not an instance this run started; refusing")
    os.kill(pid, SIGNALS[args.name])
    note(args.run_id, args.life, "signal", f"SIG{args.name} to pid {pid}")
    print(f"sent SIG{args.name} to pid {pid}")
    return 0


def cmd_dump(args: argparse.Namespace) -> int:
    require_run(args.run_id)
    store_text = store_dump(args.run_id)
    (evidence(args.run_id) / f"{args.life}.store.txt").write_text(store_text)
    log = evidence(args.run_id) / f"{args.life}.stderr.jsonl"
    counts: Counter[str] = Counter()
    trail: list[str] = []
    for line in log.read_text().splitlines() if log.exists() else []:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        event = str(record.get("event", "?"))
        counts[event] += 1
        if event.split(".")[0] in {
            "order",
            "position",
            "account",
            "engine",
            "guard",
            "ghost",
            "inflight",
            "verify",
            "valuation",
            "leverage",
            "exchange",
            "strategy",
            "reconcile",
            "feed",
        }:
            keep = {k: v for k, v in record.items() if k not in {"timestamp", "level"}}
            trail.append(json.dumps(keep, sort_keys=True))
    summary = "\n".join(f"{count:5d} {event}" for event, count in sorted(counts.items()))
    (evidence(args.run_id) / f"{args.life}.events.txt").write_text(
        summary + "\n\n--- trail\n" + "\n".join(trail) + "\n"
    )
    print(store_text)
    print("--- events")
    print(summary)
    return 0


def _observed(args: argparse.Namespace) -> str:
    """What the life shows for one check: a store cell, an event count, or the exit."""
    if args.sql:
        rows = store_query(args.run_id, args.sql)
        return str(rows[0][0]) if rows else ""
    if args.event:
        log = evidence(args.run_id) / f"{args.life}.stderr.jsonl"
        text = log.read_text() if log.exists() else ""
        return str(sum(f'"event": "{args.event}"' in line for line in text.splitlines()))
    exit_file = evidence(args.run_id) / f"{args.life}.exit"
    return exit_file.read_text().strip() if exit_file.exists() else ""


def cmd_check(args: argparse.Namespace) -> int:
    """Record one PASS or FAIL line. The line is the verdict; the exit code just
    mirrors it so a recipe can stop early."""
    require_run(args.run_id)
    got = _observed(args)
    verdict = "PASS" if got == args.expect else "FAIL"
    line = f"{verdict} {args.check_id} {args.life} expected={args.expect} got={got}"
    with (evidence(args.run_id) / "checks.txt").open("a") as sink:
        sink.write(line + "\n")
    print(line)
    return 0 if verdict == "PASS" else 1


# An event whose presence is worth a reader's attention whatever the checks say.
# ``order.rejected`` and ``order.denied`` are expected in the ghost and kill switch
# features, so a listed alarm is a prompt to look, never a verdict on its own.
ALARM_EVENTS = (
    "engine.faulted",
    "engine.stop_hook_failed",
    "strategy.error",
    "strategy.snapshot_incompatible",
    "order.rejected",
    "order.denied",
    "order.failed",
    "ghost.reconciled",
    "reconcile.frozen",
    "account.reconcile_frozen",
    "account.healed",
    "account.mode_unverified",
    "valuation.divergence",
    "leverage.divergence",
    "exchange.request_failed",
    "exchange.action_rejected",
)


def check_lines(run_id: str) -> list[str]:
    path = evidence(run_id) / "checks.txt"
    return path.read_text().splitlines() if path.exists() else []


def verdict(run_id: str) -> tuple[str, int, int]:
    """``(word, failed, total)`` over the run's recorded checks."""
    lines = check_lines(run_id)
    failed = sum(line.startswith("FAIL ") for line in lines)
    if not lines:
        return "NO CHECKS", 0, 0
    return ("FAIL" if failed else "PASS"), failed, len(lines)


def build_report(run_id: str) -> str:
    data = read_run_json(run_id)
    feature = data.get("feature") or "(no feature)"
    word, failed, total = verdict(run_id)
    headline = f"# {run_id}: {feature} {word}"
    if total:
        headline += f" ({failed} of {total} checks failed)"
    lines = [
        headline,
        "",
        f"sha {data.get('sha', '?')} on {data.get('branch', '?')}, started {data.get('started_at', '?')}",
        "",
    ]
    lines += ["## Failed checks", ""]
    fails = [line for line in check_lines(run_id) if line.startswith("FAIL ")]
    lines += [f"- {line}" for line in fails] or ["- none"]
    lines += ["", "## Alarm events", ""]
    alarms: list[str] = []
    for log in sorted(evidence(run_id).glob("*.stderr.jsonl")):
        life = log.name.removesuffix(".stderr.jsonl")
        for raw in log.read_text().splitlines():
            for name in ALARM_EVENTS:
                if f'"event": "{name}"' in raw:
                    alarms.append(f"- {life}: {raw}")
    lines += alarms or ["- none"]
    lines += ["", "## Exit codes", ""]
    exits = sorted(evidence(run_id).glob("*.exit"))
    lines += [f"- {e.stem}: {e.read_text().strip()}" for e in exits] or ["- none"]
    lines += ["", "## All checks", ""]
    lines += [f"- {line}" for line in check_lines(run_id)] or ["- none"]
    for store in sorted(evidence(run_id).glob("*.store.txt")):
        lines += [
            "",
            f"## Store after {store.name.removesuffix('.store.txt')}",
            "",
            "```",
            store.read_text().rstrip(),
            "```",
        ]
    return "\n".join(lines) + "\n"


def cmd_report(args: argparse.Namespace) -> int:
    """Write and print evidence/REPORT.md. Exit 0 only on a PASS verdict."""
    require_run(args.run_id)
    text = build_report(args.run_id)
    (evidence(args.run_id) / "REPORT.md").write_text(text)
    print(text, end="")
    return 0 if verdict(args.run_id)[0] == "PASS" else 1


def all_runs() -> list[str]:
    if not VERIFY_HOME.exists():
        return []
    return sorted(p.name for p in VERIFY_HOME.iterdir() if (p / "evidence").is_dir())


def cmd_runs(_: argparse.Namespace) -> int:
    """One line per run: id, start, feature, sha, verdict. Newest last."""
    rows = []
    for run_id in all_runs():
        data = read_run_json(run_id)
        word, failed, total = verdict(run_id)
        summary = word if not total else f"{word} {failed}/{total}"
        rows.append(
            (
                data.get("started_at", ""),
                f"{run_id:<14} {data.get('started_at', '?')[:19]}  {(data.get('feature') or '-'):<22} "
                f"{data.get('sha', '?')[:7]}  {summary}",
            )
        )
    for _, line in sorted(rows):
        print(line)
    if not rows:
        print("no runs")
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    """Delete the oldest runs past ``--keep`` per feature. Evidence goes with them,
    which is the one verb here that removes proof, so it says what it removed."""
    by_feature: dict[str, list[tuple[str, str]]] = {}
    for run_id in all_runs():
        data = read_run_json(run_id)
        by_feature.setdefault(data.get("feature") or "-", []).append(
            (data.get("started_at", ""), run_id)
        )
    for feature, runs in by_feature.items():
        for _, run_id in sorted(runs)[: max(0, len(runs) - args.keep)]:
            shutil.rmtree(run_dir(run_id))
            print(f"removed {run_id} ({feature})")
    return 0


def build_issue_draft(run_id: str) -> str | None:
    """A bug body in the shape of .github/ISSUE_TEMPLATE/bug_report.md, one per
    run, filled from the FAIL lines. ``None`` when there is nothing to file."""
    fails = [line for line in check_lines(run_id) if line.startswith("FAIL ")]
    if not fails:
        return None
    data = read_run_json(run_id)
    feature = data.get("feature") or "(no feature)"
    rel = Path(".agents/verify") / run_id / "evidence"
    lives = sorted({line.split()[2] for line in fails})
    lines = ["## What happens", ""]
    lines += [f"`/verify-tickwright` run `{run_id}` ({feature}) failed {len(fails)} check(s):", ""]
    lines += [f"- `{line}`" for line in fails]
    lines += ["", "<!-- Say what the number means in words, in one or two sentences. -->", ""]
    lines += ["## What should happen", ""]
    lines += [
        f"The expected value is the one `.claude/skills/verify-tickwright/features/{feature}.md` "
        "states for that check. Name the ADR or CONTEXT.md term behind it here.",
        "",
        "## Reproduction",
        "",
        f"1. Follow `.claude/skills/verify-tickwright/features/{feature}.md`.",
        f"2. Run `verify report {run_id}` and read the FAIL line.",
        "",
        "Evidence from this run:",
        "",
        f"- `{rel / 'REPORT.md'}`",
    ]
    lines += [
        f"- `{rel / (life + '.stderr.jsonl')}` and `{rel / (life + '.store.txt')}`"
        for life in lives
    ]
    lines += ["", "## Environment", ""]
    lines += [
        f"- Path: <!-- read {rel}/<life>.env.txt: paper + in-memory (default) | Hyperliquid testnet | Kafka bus | Postgres store -->",
        f"- Commit / branch: {data.get('sha', '?')[:7]} on {data.get('branch', '?')}",
        "",
        "## Notes",
        "",
        "<!-- If this reveals a deeper design problem, stop and escalate to a PRD instead of patching. -->",
        "",
        "<!-- Not filed. To file, after reading docs/agents/issue-tracker.md:",
        f"  gh issue create -R MarcosACH/tickwright --title '<observed defect>' --label bug --assignee @me --body-file {rel / 'ISSUE.md'}",
        "  then gh project item-add 2 --owner MarcosACH --url <issue-url>, set Status Todo, add domain labels. -->",
    ]
    return "\n".join(lines) + "\n"


def cmd_issue_draft(args: argparse.Namespace) -> int:
    """Print and save a bug draft for the run's FAILs. Never creates the issue."""
    require_run(args.run_id)
    text = build_issue_draft(args.run_id)
    if text is None:
        print(f"{args.run_id}: nothing to file, no FAIL lines")
        return 1
    (evidence(args.run_id) / "ISSUE.md").write_text(text)
    print(text, end="")
    return 0


def cmd_cloid(args: argparse.Namespace) -> int:
    from tickwright.domain import derive_cloid

    print(derive_cloid(args.signal_id))
    return 0


def cmd_infra(args: argparse.Namespace) -> int:
    require_run(args.run_id)
    data = read_run_json(args.run_id)
    if args.action == "up":
        result = subprocess.run(
            ["docker", "compose", "up", "-d", "--wait", args.service], cwd=ROOT, text=True
        )
        if result.returncode != 0:
            return result.returncode
        if args.service not in data["infra"]:
            data["infra"].append(args.service)
        write_run_json(args.run_id, data)
        if args.service == "postgres":
            import psycopg

            dbname = "verify_" + re.sub(r"[^a-z0-9]", "_", args.run_id)
            with psycopg.connect(
                "postgresql://tickwright:tickwright@localhost:5432/tickwright", autocommit=True
            ) as conn:
                conn.execute(f"drop database if exists {dbname}")
                conn.execute(f"create database {dbname}")
            print(f"dsn       postgresql://tickwright:tickwright@localhost:5432/{dbname}")
        if args.service == "kafka":
            print(
                f"topic     verify.{args.run_id}  (pass it as TICKWRIGHT_KAFKA__EVENTS_TOPIC and GROUP_ID)"
            )
        return 0
    for service in list(data.get("infra", [])):
        subprocess.run(["docker", "compose", "stop", service], cwd=ROOT, text=True)
        subprocess.run(["docker", "compose", "rm", "-f", service], cwd=ROOT, text=True)
        data["infra"].remove(service)
    write_run_json(args.run_id, data)
    return 0


def secrets_found(run_id: str) -> list[str]:
    keys = repo_env_values(KEY_VARS[:1])
    if not keys:
        return []
    needle = keys[KEY_VARS[0]]
    needles = {needle, needle.removeprefix("0x")}
    hits: list[str] = []
    for path in evidence(run_id).rglob("*"):
        if path.is_file() and any(n and n in path.read_text(errors="ignore") for n in needles):
            hits.append(str(path))
    return hits


def cmd_secrets_check(args: argparse.Namespace) -> int:
    require_run(args.run_id)
    hits = secrets_found(args.run_id)
    if hits:
        print("SIGNING KEY FOUND IN EVIDENCE:\n" + "\n".join(hits), file=sys.stderr)
        return 1
    print("no signing key in evidence")
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    require_run(args.run_id)
    for pid_file in sorted(scratch(args.run_id).glob("*.pid")):
        pid = int(pid_file.read_text().strip() or 0)
        life = pid_file.stem
        if pid and pid_alive(pid) and pid_is_ours(pid, args.run_id):
            os.kill(pid, signal.SIGTERM)
            note(args.run_id, life, "cleanup", f"SIGTERM to pid {pid}")
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and pid_alive(pid):
                time.sleep(0.1)
            if pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
                note(args.run_id, life, "cleanup", f"SIGKILL to pid {pid}")
    infra_args = argparse.Namespace(run_id=args.run_id, action="down", service=None)
    cmd_infra(infra_args)
    hits = secrets_found(args.run_id)
    if hits:
        print(
            "SIGNING KEY FOUND IN EVIDENCE, deleting those files:\n" + "\n".join(hits),
            file=sys.stderr,
        )
        for hit in hits:
            Path(hit).unlink()
    shutil.rmtree(scratch(args.run_id), ignore_errors=True)
    data = read_run_json(args.run_id)
    data["cleaned_at"] = now_iso()
    write_run_json(args.run_id, data)
    kept = sorted(p.name for p in evidence(args.run_id).iterdir())
    print(f"scratch removed. evidence kept at {evidence(args.run_id)}:")
    print("\n".join(f"  {name}" for name in kept))
    return 1 if hits else 0


# --- argument parsing --------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="verify", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="read-only health of this checkout").set_defaults(fn=cmd_doctor)

    p = sub.add_parser("init", help="create .agents/verify/<run-id>/{scratch,evidence}")
    p.add_argument("run_id")
    p.add_argument("--feature", default=None, help="the feature file this run covers")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("ticks", help="write a replay tick file into scratch/")
    p.add_argument("run_id")
    p.add_argument("name", help="file stem, written as scratch/<name>.jsonl")
    p.add_argument("--base", default="2024-01-01T00:00:00+00:00", help="ISO timestamp of offset 0")
    p.add_argument(
        "--row", action="append", required=True, help="SYMBOL:PRICE[:SIZE[:SIDE]]@OFFSET"
    )
    p.set_defaults(fn=cmd_ticks)

    p = sub.add_parser("start", help="spawn one engine life in the background")
    p.add_argument("run_id")
    p.add_argument("life", help="a name for this process, e.g. first, second")
    p.add_argument("--preset", choices=sorted(PRESETS), help="baseline .env to start from")
    p.add_argument("--env", action="append", default=[], help="KEY=VALUE written to scratch/.env")
    p.add_argument("--strategy", help="run scripts/run_strategy.py <name> instead of the CLI")
    p.add_argument("--param", action="append", default=[], help="KEY=VALUE exported as VERIFY_KEY")
    p.add_argument(
        "--forward-key", action="store_true", help="forward the testnet key from the repo .env"
    )
    p.set_defaults(fn=cmd_start)

    p = sub.add_parser("_supervise")
    p.add_argument("run_id")
    p.add_argument("life")
    p.add_argument("command", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_supervise)

    p = sub.add_parser("await", help="poll until a log event, a store state, or the exit")
    p.add_argument("run_id")
    p.add_argument("life")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--event", help="named event, e.g. order.filled")
    group.add_argument("--order", help="CLOID=STATE in the orders table")
    group.add_argument("--sql", help="a query whose first cell must equal --expect")
    group.add_argument("--exit", action="store_true", help="the process ended")
    p.add_argument("--count", type=int, default=1, help="with --event: the Nth occurrence")
    p.add_argument("--expect", default="", help="with --sql: the expected first cell")
    p.add_argument("--timeout", type=float, default=30.0)
    p.set_defaults(fn=cmd_await)

    p = sub.add_parser("signal", help="send an operator signal to a life")
    p.add_argument("run_id")
    p.add_argument("life")
    p.add_argument("name", choices=sorted(SIGNALS))
    p.set_defaults(fn=cmd_signal)

    p = sub.add_parser("check", help="record PASS or FAIL for one expected value")
    p.add_argument("run_id")
    p.add_argument("life")
    p.add_argument("check_id", help="the sub-feature id from the feature file, e.g. paper-fill")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--sql", help="a query whose first cell is compared")
    group.add_argument("--event", help="a named event whose count is compared")
    group.add_argument("--exit", action="store_true", help="the exit code is compared")
    p.add_argument("--expect", required=True, help="the value the feature file says")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("dump", help="write the store and event summary into evidence/")
    p.add_argument("run_id")
    p.add_argument("life")
    p.set_defaults(fn=cmd_dump)

    p = sub.add_parser("report", help="write evidence/REPORT.md: verdict, fails, alarms, exits")
    p.add_argument("run_id")
    p.set_defaults(fn=cmd_report)

    sub.add_parser("runs", help="one line per run with its verdict").set_defaults(fn=cmd_runs)

    p = sub.add_parser("prune", help="delete the oldest runs past --keep per feature")
    p.add_argument("--keep", type=int, default=5)
    p.set_defaults(fn=cmd_prune)

    p = sub.add_parser("issue-draft", help="print a bug body for the run's FAIL lines")
    p.add_argument("run_id")
    p.set_defaults(fn=cmd_issue_draft)

    p = sub.add_parser("cloid", help="the cloid a signal_id maps to")
    p.add_argument("signal_id", help="<strategy_id>:<symbol>:<seq>")
    p.set_defaults(fn=cmd_cloid)

    p = sub.add_parser("infra", help="start or stop the docker services this run owns")
    p.add_argument("run_id")
    p.add_argument("action", choices=["up", "down"])
    p.add_argument("service", nargs="?", choices=["postgres", "kafka"])
    p.set_defaults(fn=cmd_infra)

    p = sub.add_parser("secrets-check", help="fail if the signing key appears in evidence")
    p.add_argument("run_id")
    p.set_defaults(fn=cmd_secrets_check)

    p = sub.add_parser("cleanup", help="stop this run's instances, drop scratch, keep evidence")
    p.add_argument("run_id")
    p.set_defaults(fn=cmd_cleanup)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "infra" and args.action == "up" and not args.service:
        sys.exit("infra up needs a service: postgres or kafka")
    if args.command == "_supervise":
        args.command = [c for c in args.command if c != "--"]
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
