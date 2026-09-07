"""The CLI E2E (issue #19 acceptance): a real process, run from ``.env``.

``python -m tickwright.app`` in a scratch directory: replays the configured
file, trades on the paper venue, and exits 0 on SIGTERM. The resting LIVE
order is still checkpointed afterwards — a graceful stop never cancels it —
and the next start reconciles it against venue truth. (The paper venue's book
dies with its process, so the second life's read comes back empty — and the
barrier arms the ghost grace window rather than concluding on it, leaving the
order as recovered (ADR-0011 inv 3, as amended by #243). It stays that way for
this process's whole life: a replay run is on virtual time, and once the tick
file is exhausted there is no clock left to spend a grace window against. True
re-adoption, where the venue survives, is proven in-process in
``tests/engine/test_runner_e2e.py``.)
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tickwright.adapters.store import SQLiteStore
from tickwright.domain import OrderState, derive_cloid

_FILLED_CLOID = derive_cloid("shooter:BTC:1")
_RESTING_CLOID = derive_cloid("rester:ETH:1")

# One symbol per strategy: ADR-0034's disjointness rule forbids two strategies
# over one symbol on a NET venue, so the two kinds get a symbol each.
_SPECS = {
    symbol: {
        "symbol": symbol,
        "sz_decimals": 3,
        "max_decimals": 6,
        "max_sig_figs": 5,
        "min_notional": "10",
    }
    for symbol in ("BTC", "ETH")
}
_STRATEGIES = [
    {
        "kind": "single_shot_market",
        "strategy_id": "shooter",
        "symbol": "BTC",
        "side": "buy",
        "quantity": "0.5",
    },
    {
        "kind": "single_shot_limit",
        "strategy_id": "rester",
        "symbol": "ETH",
        "side": "buy",
        "quantity": "0.5",
        "price": "41000",
    },
]
_TICKS = [
    {
        "symbol": symbol,
        "price": "42000",
        "size": "3",
        "aggressor_side": "buy",
        "trade_id": trade_id,
        "ts_event": ts_event,
    }
    for symbol, trade_id, ts_event in (("BTC", "a", 1_000), ("ETH", "b", 1_001))
]


def _write_workspace(cwd: Path) -> None:
    (cwd / "ticks.jsonl").write_text("\n".join(json.dumps(t) for t in _TICKS) + "\n")
    (cwd / ".env").write_text(
        "TICKWRIGHT_REPLAY__PATH=ticks.jsonl\n"
        "TICKWRIGHT_SQLITE__PATH=saga.db\n"
        "TICKWRIGHT_PAPER__GENESIS_COLLATERAL=100000\n"
        f"TICKWRIGHT_PAPER__INSTRUMENT_SPECS={json.dumps(_SPECS)}\n"
        f"TICKWRIGHT_STRATEGIES={json.dumps(_STRATEGIES)}\n"
    )


def _export_hostile_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Export a live-venue config into the shell this test's CLI inherits.

    The rest of ``tests/app/`` is hermetic because it builds the pure
    ``AppConfig``, but this test's whole subject is a real process whose job is
    to read the environment — no config class can insulate it. A developer (or
    a CI ``env:`` block) with these exported must not change what the CLI under
    test does, so the test exports them itself and expects the spawn to scrub
    them (issue #71).
    """
    monkeypatch.setenv("TICKWRIGHT_FEED", "hyperliquid")
    monkeypatch.setenv("TICKWRIGHT_EXCHANGE", "hyperliquid")
    monkeypatch.setenv("TICKWRIGHT_HYPERLIQUID__SYMBOLS", '["BTC"]')
    monkeypatch.setenv("TICKWRIGHT_HYPERLIQUID__SIGNING_KEY", "0xdeadbeef")


def _spawn(cwd: Path, log: str) -> subprocess.Popen[bytes]:
    """The CLI under test, configured only by the ``.env`` in ``cwd``.

    The child inherits the shell otherwise, and exported ``TICKWRIGHT_*`` vars
    outrank the ``.env`` this test wrote — so they go. The file half needs no
    scrubbing and must keep working: the CLI resolves the relative ``.env``
    against the child's cwd, which is ``tmp_path``.

    The log goes to a **file** rather than a pipe so it can be read while the
    child is still running. One life's readiness is otherwise unobservable: a
    restart that correctly resolves nothing writes nothing to the store, so
    there is no durable state to poll and the only signal that boot finished is
    the child saying so.
    """
    # Closed as soon as the child has its own duplicate of the descriptor —
    # the parent holding it open buys nothing and leaks a handle per life.
    with (cwd / log).open("wb") as sink:
        return subprocess.Popen(
            [sys.executable, "-m", "tickwright.app"],
            cwd=cwd,
            env={k: v for k, v in os.environ.items() if not k.startswith("TICKWRIGHT_")},
            stdout=subprocess.PIPE,
            stderr=sink,
        )


def _await_event(log: Path, event: str, *, timeout: float = 15.0) -> None:
    """Poll a running child's log until it emits ``event``."""
    deadline = time.monotonic() + timeout
    needle = f'"event": "{event}"'
    while time.monotonic() < deadline:
        if log.exists() and needle in log.read_text():
            return
        time.sleep(0.05)
    raise AssertionError(f"log never emitted {event}:\n{log.read_text() if log.exists() else ''}")


def _await_states(db: Path, wanted: dict[str, OrderState], *, timeout: float = 15.0) -> None:
    """Poll the durable store until every cloid reaches its wanted state."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if db.exists():
            reader = SQLiteStore(db)
            try:
                orders = {cloid: reader.get_order(cloid) for cloid in wanted}
            finally:
                reader.close()
            if all(o is not None and o.state is wanted[c] for c, o in orders.items()):
                return
        time.sleep(0.05)
    raise AssertionError(f"store never reached {wanted}")


def _terminate(process: subprocess.Popen[bytes], *, timeout: float = 15.0) -> int:
    process.send_signal(signal.SIGTERM)
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        raise


def test_cli_replays_trades_and_exits_zero_on_sigterm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_workspace(tmp_path)
    _export_hostile_config(monkeypatch)
    db = tmp_path / "saga.db"

    first_log = tmp_path / "first.log"
    second_log = tmp_path / "second.log"

    with _spawn(tmp_path, first_log.name) as process:
        try:
            # First life: the market shot fills, the low limit rests LIVE.
            _await_states(db, {_FILLED_CLOID: OrderState.FILLED, _RESTING_CLOID: OrderState.LIVE})
            assert process.poll() is None, "engine must keep running after replay end-of-file"
        except BaseException:
            process.kill()
            raise AssertionError(f"first life failed; stderr:\n{first_log.read_text()}") from None

        assert _terminate(process) == 0

    # The graceful stop left the resting LIVE order checkpointed, untouched.
    reader = SQLiteStore(db)
    try:
        resting = reader.get_order(_RESTING_CLOID)
        assert resting is not None
        assert resting.state is OrderState.LIVE
    finally:
        reader.close()

    # Next start: the barrier reconciles the survivor against venue truth
    # before anything can trade. This paper venue lost its book with the old
    # process, so the read comes back empty — and an empty read is not proof at
    # boot any more than it is in flight (ADR-0011 inv 3, as amended by #243).
    # The barrier arms the grace window and clears; the order is left exactly
    # as recovered, and the restored strategies stay quiet (no re-placement).
    with _spawn(tmp_path, second_log.name) as second:
        try:
            _await_event(second_log, "engine.barrier_cleared")
        except BaseException:
            second.kill()
            raise AssertionError(f"second life failed; stderr:\n{second_log.read_text()}") from None
        assert _terminate(second) == 0

    # Boot resolved nothing terminally on one read — the whole of #243. And it
    # never will *in this process*: a replay run is on virtual time (ADR-0027/
    # 0033), so once the tick file is exhausted nothing advances the clock and
    # the open-order cadence's deadline never comes. The grace window is real
    # time passing, and a finished replay has none left to spend. A live run,
    # where the wall clock moves on its own, ghosts it one window later —
    # proven on virtual time in ``tests/engine/test_reconcile.py``.
    assert "ghost.reconciled" not in second_log.read_text()
    reader = SQLiteStore(db)
    try:
        survivor = reader.get_order(_RESTING_CLOID)
        assert survivor is not None
        assert survivor.state is OrderState.LIVE
        assert reader.get_order(derive_cloid("shooter:BTC:2")) is None
        assert reader.get_order(derive_cloid("rester:ETH:2")) is None
    finally:
        reader.close()
