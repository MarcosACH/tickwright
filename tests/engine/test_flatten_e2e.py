"""Operator flatten (ADR-0053, ADR-0054, ADR-0056, ADR-0060): the ``Engine`` as a
one-shot exit run that closes every position.

The real paper path, with no strategy: ``PaperExchange`` over ``InMemoryBus`` and
``SQLiteStore``, fed by ``ReplayFeed`` on ``ManualClock``.
"""

import asyncio
import json
from pathlib import Path

from ledgers import GENESIS

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.feed import ReplayFeed
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.adapters.store import SQLiteStore
from tickwright.engine.exit_run import Flatten
from tickwright.engine.guard import RealGuard
from tickwright.engine.runner import Engine
from tickwright.observability.testing import capture_events


def _ticks(path: Path) -> Path:
    row = {
        "symbol": "BTC",
        "price": "42000",
        "size": "1",
        "aggressor_side": "buy",
        "trade_id": "a",
        "ts_event": 1_000,
    }
    path.write_text(json.dumps(row) + "\n")
    return path


def test_flatten_with_nothing_to_close_still_trips_the_kill_switch(tmp_path: Path) -> None:
    # The trip records what the operator meant, not what flatten found (ADR-0053).
    bus = InMemoryBus()
    clock = ManualClock()
    store = SQLiteStore(tmp_path / "saga.db")
    paper = PaperExchange(
        bus=bus,
        clock=clock,
        fill_model=ImmediateFillModel(),
        genesis_collateral=GENESIS,
        account_net=dict,
        applied_fills=lambda cloid: (),
    )
    engine = Engine(
        bus=bus,
        clock=clock,
        store=store,
        exchange=paper,
        feed=ReplayFeed(path=_ticks(tmp_path / "ticks.jsonl"), bus=bus, clock=clock),
        guard=RealGuard(specs=paper.instrument_specs(), store=store, clock=clock),
        exit_job=Flatten(),
    )

    with capture_events() as logs:
        exit_code = asyncio.run(asyncio.wait_for(engine.run(), timeout=5))

    assert exit_code == 0
    events = [log["event"] for log in logs]
    assert "guard.kill_switch_tripped" in events
    assert "guard.kill_switch_reset" not in events
    assert logs[-1]["event"] == "exit.finished"
    assert logs[-1]["job"] == "flatten"
    assert logs[-1]["outcome"] == "done"
    assert logs[-1]["left"] == "none"
    after = SQLiteStore(tmp_path / "saga.db")
    try:
        state = after.load_kill_switch()
    finally:
        after.close()
    assert state is not None
    assert state.tripped
    assert state.reason == "flatten: operator exit"
