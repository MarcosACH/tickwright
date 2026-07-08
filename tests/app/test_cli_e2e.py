"""The CLI E2E (issue #19 acceptance): a real process, run from ``.env``.

``python -m tickwright.app`` in a scratch directory: replays the configured
file, trades on the paper venue, and exits 0 on SIGTERM. The resting LIVE
order is still checkpointed afterwards — a graceful stop never cancels it —
and the next start reconciles it against venue truth. (The paper venue's book
dies with its process, so the second life's barrier ghost-resolves the order
REJECTED — positive proof it is gone, never a guess (ADR-0010/0011). True
re-adoption, where the venue survives, is proven in-process in
``tests/engine/test_runner_e2e.py``.)
"""

import json
import signal
import subprocess
import sys
import time
from pathlib import Path

from tickwright.adapters.store import SQLiteStore
from tickwright.domain import OrderState, derive_cloid

_FILLED_CLOID = derive_cloid("shooter:BTC:1")
_RESTING_CLOID = derive_cloid("rester:BTC:1")

_SPECS = {
    "BTC": {
        "symbol": "BTC",
        "sz_decimals": 3,
        "max_decimals": 6,
        "max_sig_figs": 5,
        "min_notional": "10",
    }
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
        "symbol": "BTC",
        "side": "buy",
        "quantity": "0.5",
        "price": "41000",
    },
]
_TICKS = [
    {
        "symbol": "BTC",
        "price": "42000",
        "size": "3",
        "aggressor_side": "buy",
        "trade_id": "a",
        "ts_event": 1_000,
    }
]


def _write_workspace(cwd: Path) -> None:
    (cwd / "ticks.jsonl").write_text("\n".join(json.dumps(t) for t in _TICKS) + "\n")
    (cwd / ".env").write_text(
        "TICKWRIGHT_REPLAY__PATH=ticks.jsonl\n"
        "TICKWRIGHT_SQLITE__PATH=saga.db\n"
        f"TICKWRIGHT_PAPER__INSTRUMENT_SPECS={json.dumps(_SPECS)}\n"
        f"TICKWRIGHT_STRATEGIES={json.dumps(_STRATEGIES)}\n"
    )


def _spawn(cwd: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-m", "tickwright.app"],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


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


def test_cli_replays_trades_and_exits_zero_on_sigterm(tmp_path: Path) -> None:
    _write_workspace(tmp_path)
    db = tmp_path / "saga.db"

    with _spawn(tmp_path) as process:
        try:
            # First life: the market shot fills, the low limit rests LIVE.
            _await_states(db, {_FILLED_CLOID: OrderState.FILLED, _RESTING_CLOID: OrderState.LIVE})
            assert process.poll() is None, "engine must keep running after replay end-of-file"
        except BaseException:
            process.kill()
            _, stderr = process.communicate()
            raise AssertionError(f"first life failed; stderr:\n{stderr.decode()}") from None

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
    # process, so positive venue proof ghost-resolves the LIVE order REJECTED
    # (ADR-0010) — and the restored strategies stay quiet (no re-placement).
    with _spawn(tmp_path) as second:
        try:
            _await_states(db, {_RESTING_CLOID: OrderState.REJECTED})
        except BaseException:
            second.kill()
            _, stderr = second.communicate()
            raise AssertionError(f"second life failed; stderr:\n{stderr.decode()}") from None
        assert _terminate(second) == 0

    reader = SQLiteStore(db)
    try:
        assert reader.get_order(derive_cloid("shooter:BTC:2")) is None
        assert reader.get_order(derive_cloid("rester:BTC:2")) is None
    finally:
        reader.close()
