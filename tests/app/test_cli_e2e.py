"""The CLI E2E (issue #19 acceptance): a real process, run from ``.env``.

``python -m tickwright.app`` in a scratch directory: replays the configured
file, trades on the paper venue, and exits 0 on SIGTERM. The resting LIVE
order is still checkpointed afterwards — a graceful stop never cancels it —
and the next start reconciles it against venue truth. The new paper venue
rebuilds its book from the store, so the next start finds the order and
re-adopts it, as a live venue that kept it would (ADR-0012, #484).
"""

import errno
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from ledgers import checkpointer
from store_backends import PostgresBackend, resolve_backend

from tickwright.adapters.clock import ManualClock
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


def _spawn(cwd: Path, log: Path, *args: str) -> subprocess.Popen[bytes]:
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

    Nothing reads stdout, so it goes to ``DEVNULL`` rather than a pipe: an
    undrained ``PIPE`` would deadlock ``_terminate``'s ``wait()`` the day the
    CLI prints anything there, and logging factories onto stderr regardless.
    """
    # Closed as soon as the child has its own duplicate of the descriptor —
    # the parent holding it open buys nothing and leaks a handle per life.
    with log.open("wb") as sink:
        return subprocess.Popen(
            [sys.executable, "-m", "tickwright.app", *args],
            cwd=cwd,
            env={k: v for k, v in os.environ.items() if not k.startswith("TICKWRIGHT_")},
            stdout=subprocess.DEVNULL,
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

    with _spawn(tmp_path, first_log) as process:
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
    # before anything can trade. The new paper venue rebuilt the order from
    # the store, so the read finds it and the barrier re-adopts it (#484). The
    # restored strategies stay quiet (no re-placement).
    with _spawn(tmp_path, second_log) as second:
        try:
            _await_event(second_log, "engine.barrier_cleared")
        except BaseException:
            second.kill()
            raise AssertionError(f"second life failed; stderr:\n{second_log.read_text()}") from None
        assert _terminate(second) == 0

    # A replay run never moves the clock past the grace window, so this alone
    # proves little. tests/engine/test_paper_restart_e2e.py drives reconcile
    # past the window and proves the rebuilt order is never ghosted.
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


def test_a_second_engine_on_the_same_store_exits_1_until_the_first_is_killed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One engine owns a store at a time (ADR-0052). A second one exits 1 and
    names the first one's pid, and the first keeps running. The lock dies with
    its process, so after a SIGKILL a new engine starts on the same store."""
    _write_workspace(tmp_path)
    _export_hostile_config(monkeypatch)

    with _spawn(tmp_path, tmp_path / "first.log") as first:
        try:
            _await_event(tmp_path / "first.log", "engine.barrier_cleared")

            second_log = tmp_path / "second.log"
            with _spawn(tmp_path, second_log) as second:
                try:
                    exit_code = second.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    second.kill()
                    raise AssertionError("the second engine ran on a held store") from None
                assert exit_code == 1
            assert "engine.faulted" in second_log.read_text()
            assert f"Process {first.pid} holds" in second_log.read_text()
            assert first.poll() is None, "the first engine must keep running"
        finally:
            first.kill()
        first.wait(timeout=15)

    third_log = tmp_path / "third.log"
    with _spawn(tmp_path, third_log) as third:
        try:
            _await_event(third_log, "engine.barrier_cleared")
        except BaseException:
            third.kill()
            raise AssertionError(f"restart failed; stderr:\n{third_log.read_text()}") from None
        assert _terminate(third) == 0


def _run_to_exit(cwd: Path, log: Path, *args: str, timeout: float = 15.0) -> int:
    """Run a one-shot CLI command and return its exit code."""
    with _spawn(cwd, log, *args) as process:
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            raise AssertionError(f"{args} never exited; stderr:\n{log.read_text()}") from None


def _events(log: Path) -> list[dict[str, object]]:
    """Every JSON event in a child's log, in order."""
    lines = log.read_text().splitlines()
    return [json.loads(line) for line in lines if line.startswith("{")]


def test_cancel_all_on_an_empty_paper_account_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_workspace(tmp_path)
    _export_hostile_config(monkeypatch)
    log = tmp_path / "cancel_all.log"
    # An engine booted here before, so the store holds the account row (ADR-0052).
    with SQLiteStore(tmp_path / "saga.db") as store:
        checkpointer(store, clock=ManualClock()).recover()

    assert _run_to_exit(tmp_path, log, "cancel-all") == 0, log.read_text()

    events = _events(log)
    assert events[-1]["event"] == "exit.finished"
    assert events[-1]["job"] == "cancel_all"
    assert events[-1]["left"] == "none"
    # The configured strategies belong to normal runs. This run places nothing.
    assert not any(str(event["event"]).startswith("order.") for event in events)


def test_cancel_all_after_a_paper_run_cancels_the_order_it_left_resting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The #484 repro. A paper run leaves a limit resting and stops. A
    ``cancel-all`` on the same store must find that order and end it CANCELLED,
    the way it would on a live venue that kept the order."""
    _write_workspace(tmp_path)
    _export_hostile_config(monkeypatch)
    db = tmp_path / "saga.db"
    run_log = tmp_path / "run.log"

    with _spawn(tmp_path, run_log) as process:
        try:
            _await_states(db, {_FILLED_CLOID: OrderState.FILLED, _RESTING_CLOID: OrderState.LIVE})
        except BaseException:
            process.kill()
            raise AssertionError(f"the run failed; stderr:\n{run_log.read_text()}") from None
        assert _terminate(process) == 0

    log = tmp_path / "cancel_all.log"
    assert _run_to_exit(tmp_path, log, "cancel-all") == 0, log.read_text()

    events = _events(log)
    assert events[-1]["event"] == "exit.finished"
    assert events[-1]["left"] == "none"
    with SQLiteStore(db) as reader:
        resting = reader.get_order(_RESTING_CLOID)
        filled = reader.get_order(_FILLED_CLOID)
    assert resting is not None
    assert resting.state is OrderState.CANCELLED
    assert filled is not None
    assert filled.state is OrderState.FILLED


def test_cancel_all_on_a_store_another_engine_holds_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_workspace(tmp_path)
    _export_hostile_config(monkeypatch)
    log = tmp_path / "cancel_all.log"
    with SQLiteStore(tmp_path / "saga.db") as holder:
        assert holder.lock() is None

        assert _run_to_exit(tmp_path, log, "cancel-all") == 2, log.read_text()

    assert [event["event"] for event in _events(log)] == ["exit.refused"]


def test_cancel_all_on_a_store_no_engine_has_used_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_workspace(tmp_path)
    _export_hostile_config(monkeypatch)
    log = tmp_path / "cancel_all.log"

    assert _run_to_exit(tmp_path, log, "cancel-all") == 2, log.read_text()

    events = _events(log)
    assert [event["event"] for event in events] == ["exit.refused"]
    assert events[0]["reason"] == "new_store"
    with SQLiteStore(tmp_path / "saga.db") as store:
        assert store.load_account() is None


def test_cancel_all_with_new_store_on_a_store_no_engine_has_used_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_workspace(tmp_path)
    _export_hostile_config(monkeypatch)
    log = tmp_path / "cancel_all.log"

    assert _run_to_exit(tmp_path, log, "cancel-all", "--new-store") == 0, log.read_text()

    events = [event["event"] for event in _events(log)]
    assert events.count("exit.new_store") == 1
    assert events[-1] == "exit.finished"


def test_new_store_with_no_exit_command_exits_two_and_starts_no_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The flag means nothing to a trading run, so a stray one must not start one.
    _write_workspace(tmp_path)
    _export_hostile_config(monkeypatch)
    log = tmp_path / "engine.log"

    assert _run_to_exit(tmp_path, log, "--new-store") == 2, log.read_text()

    assert "--new-store" in log.read_text()
    assert _events(log) == []


def test_cancel_all_on_a_store_it_cannot_open_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store fails before boot, so nothing at the venue moved (ADR-0060)."""
    _write_workspace(tmp_path)
    _export_hostile_config(monkeypatch)
    with (tmp_path / ".env").open("a") as env:
        env.write("TICKWRIGHT_SQLITE__PATH=no-such-dir/saga.db\n")
    log = tmp_path / "cancel_all.log"

    assert _run_to_exit(tmp_path, log, "cancel-all") == 2, log.read_text()

    assert "tickwright cancel-all:" in log.read_text()
    assert _events(log) == []


def test_flatten_on_an_empty_paper_account_trips_the_kill_switch_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_workspace(tmp_path)
    _export_hostile_config(monkeypatch)
    log = tmp_path / "flatten.log"
    # An engine booted here before, so the store holds the account row (ADR-0052).
    with SQLiteStore(tmp_path / "saga.db") as store:
        checkpointer(store, clock=ManualClock()).recover()

    assert _run_to_exit(tmp_path, log, "flatten") == 0, log.read_text()

    events = _events(log)
    assert "guard.kill_switch_tripped" in [event["event"] for event in events]
    assert events[-1]["event"] == "exit.finished"
    assert events[-1]["job"] == "flatten"
    assert events[-1]["left"] == "none"


def test_flatten_on_hyperliquid_priced_by_a_replay_exits_two_before_it_boots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replay row is not the market, so a real close must not be priced from one.
    The run refuses before it connects, and nothing at the venue moves."""
    _write_workspace(tmp_path)
    with (tmp_path / ".env").open("a") as env:
        env.write(
            "TICKWRIGHT_EXCHANGE=hyperliquid\n"
            'TICKWRIGHT_HYPERLIQUID__SYMBOLS=["BTC"]\n'
            "TICKWRIGHT_HYPERLIQUID__SIGNING_KEY=0x" + "11" * 32 + "\n"
        )
    log = tmp_path / "flatten.log"

    assert _run_to_exit(tmp_path, log, "flatten") == 2, log.read_text()

    assert "needs feed=hyperliquid" in log.read_text()
    assert _events(log) == []


def _open_tick_pipe(fifo: Path, *, timeout: float = 15.0) -> int:
    """Open the write end of the engine's tick pipe once the engine reads it.

    Non-blocking, so a child that dies before it reaches its feed fails the test
    instead of hanging it. Until a reader opens the pipe, the open fails with
    ``ENXIO``.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as exc:
            if exc.errno != errno.ENXIO:
                raise
        time.sleep(0.05)
    raise AssertionError("the engine never opened its tick file")


@pytest.mark.postgres
def test_an_engine_whose_postgres_session_ends_exits_1_before_its_next_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An engine that lost its session has lost its lock, and another engine may
    hold it now. It never reconnects, so its next store call fails and the
    engine stops before it sends the order (ADR-0052).

    On paper that call is the exchange's read of applied fills, not the saga's
    ``PENDING`` write. A live venue has no such read, so the write is what stops
    it there. ``test_a_postgres_store_that_lost_its_session_refuses_its_next_write``
    covers that write.

    The tick file is a named pipe, so the engine waits on it like a live feed.
    That gives the test a moment between two ticks to end the session."""
    backend = resolve_backend("postgres", tmp_path / "unused.db")
    assert isinstance(backend, PostgresBackend)
    _write_workspace(tmp_path)
    os.mkfifo(tmp_path / "ticks.fifo")
    late = {**_STRATEGIES[0], "strategy_id": "late", "symbol": "ETH"}
    with (tmp_path / ".env").open("a") as env:
        env.write(
            "TICKWRIGHT_REPLAY__PATH=ticks.fifo\n"
            "TICKWRIGHT_STORE=postgres\n"
            f"TICKWRIGHT_POSTGRES__DSN={backend.dsn}\n"
            f"TICKWRIGHT_STRATEGIES={json.dumps([_STRATEGIES[0], late])}\n"
        )
    _export_hostile_config(monkeypatch)
    log = tmp_path / "engine.log"
    late_cloid = derive_cloid("late:ETH:1")

    with _spawn(tmp_path, log) as engine:
        try:
            pipe = _open_tick_pipe(tmp_path / "ticks.fifo")
            try:
                os.write(pipe, (json.dumps(_TICKS[0]) + "\n").encode())
                _await_event(log, "order.filled")
                with backend.open() as probe:
                    holder = probe.lock()
                assert holder is not None and holder.pid is not None

                backend.terminate(holder.pid)
                os.write(pipe, (json.dumps(_TICKS[1]) + "\n").encode())

                exit_code = engine.wait(timeout=15)
            finally:
                os.close(pipe)
        except BaseException:
            engine.kill()
            raise AssertionError(f"engine run failed; stderr:\n{log.read_text()}") from None

    assert exit_code == 1, log.read_text()
    events = _events(log)
    assert "engine.faulted" in [event["event"] for event in events]
    submitted = [e["signal_id"] for e in events if e["event"] == "order.submitted"]
    assert submitted == ["shooter:BTC:1"]
    with backend.open() as reader:
        assert reader.get_order(late_cloid) is None
        assert reader.lock() is None
