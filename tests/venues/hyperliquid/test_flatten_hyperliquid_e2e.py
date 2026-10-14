"""Flatten on Hyperliquid, end to end (issue #475): the real ``Engine`` and
``Flatten`` against the real ``HyperliquidExchange`` and ``HyperliquidFeed``,
with only the HTTP transport and the WebSocket fake.

On real money the account can hold a coin the operator never configured, opened
by hand in the venue UI. Flatten must price that coin and close it.
"""

import json
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

import pytest
from hyperliquid_fakes import FakeExchangeApi, FakeWsConnection, request_type, trade, trades_frame
from hyperliquid_flatten import (
    ADDRESS_LIMIT,
    HYPE_ASSET,
    START_MS,
    HeldShort,
    RateLimitedOnce,
    priced_frames,
    run_flatten,
)

from tickwright.observability.testing import capture_events
from tickwright.venues.hyperliquid.transport import IpRateLimited


def _flatten(
    tmp_path: Path, venue: HeldShort, frames: list[str]
) -> tuple[int, list[MutableMapping[str, Any]], FakeExchangeApi, FakeWsConnection]:
    """Run ``flatten`` on Hyperliquid and keep the named events it emitted."""
    with capture_events() as logs:
        exit_code, post, socket = run_flatten(tmp_path / "saga.db", venue, frames)
    return exit_code, logs, post, socket


def test_flatten_subscribes_and_closes_a_held_coin_the_config_never_named(
    tmp_path: Path,
) -> None:
    exit_code, logs, post, socket = _flatten(tmp_path, HeldShort(), priced_frames())

    assert exit_code == 0, [(log["event"], log.get("error")) for log in logs]
    subscribed = {json.loads(m)["subscription"]["coin"] for m in socket.sent}
    assert "HYPE" in subscribed
    (order,) = [
        payload["action"]["orders"][0]
        for url, payload in post.requests
        if request_type(url, payload) == "order"
    ]
    # Buys back the 2.0 short, and can only reduce it.
    assert (order["a"], order["b"], order["s"], order["r"]) == (HYPE_ASSET, True, "2", True)
    assert not [log for log in logs if log["event"] == "flatten.dry_attempt"]
    assert logs[-1]["event"] == "exit.finished"
    assert logs[-1]["outcome"] == "done"


@pytest.mark.parametrize(
    ("refusal", "limit"),
    [
        ({"status": "err", "response": ADDRESS_LIMIT}, "address"),
        (IpRateLimited("429 from the venue"), "ip"),
    ],
)
def test_a_close_refused_for_a_rate_limit_waits_10_s_and_is_no_dry_attempt(
    tmp_path: Path, refusal: object, limit: str
) -> None:
    # The venue lets requests through again in time, so waiting makes progress.
    # A dry attempt here would let a busy account give up a close it could make.
    exit_code, logs, post, _ = _flatten(tmp_path, RateLimitedOnce(refusal), priced_frames())

    assert exit_code == 0, [(log["event"], log.get("error")) for log in logs]
    limited = [log for log in logs if log["event"] == "flatten.rate_limited"]
    assert [(log["symbol"], log["limit"]) for log in limited] == [("HYPE", limit)]
    assert not [log for log in logs if log["event"] == "flatten.dry_attempt"]
    # The nonce is the clock's ms at the send, so it dates each close.
    first, second = [
        payload["nonce"] for url, payload in post.requests if request_type(url, payload) == "order"
    ]
    assert second - first >= 10_000
    assert logs[-1]["outcome"] == "done"


def test_a_coin_with_trades_but_no_mark_is_never_closed(tmp_path: Path) -> None:
    # Hyperliquid caps a market order by the mark. With no mark, the close
    # cannot be priced, so each attempt is dry and no order goes out (#467).
    frames = [trades_frame(trade("HYPE", "25.0", 7, time=START_MS))]

    exit_code, logs, post, _ = _flatten(tmp_path, HeldShort(), frames)

    assert exit_code == 1
    assert not [url for url, payload in post.requests if request_type(url, payload) == "order"]
    dry = [log for log in logs if log["event"] == "flatten.dry_attempt"]
    assert [(log["symbol"], log["count"], log["reason"]) for log in dry] == [
        ("HYPE", 1, "no_price"),
        ("HYPE", 2, "no_price"),
        ("HYPE", 3, "no_price"),
    ]
    assert logs[-1]["event"] == "exit.finished"
    assert logs[-1]["outcome"] == "stopped"
    assert logs[-1]["left"] == "HYPE=-2.0"
