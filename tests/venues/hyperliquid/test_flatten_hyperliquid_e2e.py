"""Flatten on Hyperliquid, end to end (issue #475): the real ``Engine`` and
``Flatten`` against the real ``HyperliquidExchange`` and ``HyperliquidFeed``,
with only the HTTP transport and the WebSocket fake.

On real money the account can hold a coin the operator never configured, opened
by hand in the venue UI. Flatten must price that coin and close it.
"""

import json
from collections.abc import MutableMapping
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hyperliquid_fakes import (
    FakeExchangeApi,
    FakeWsConnection,
    order_status_response,
    request_type,
    trade,
    trades_frame,
)
from hyperliquid_flatten import (
    ADDRESS_LIMIT,
    CLOSE_OID,
    HYPE_ASSET,
    START_MS,
    AnswersFirst,
    HeldShort,
    priced_frames,
    run_flatten,
)

from tickwright.adapters.store import SQLiteStore
from tickwright.domain import OPERATOR, OrderState, derive_cloid
from tickwright.engine.reconcile import ReconcileConfig
from tickwright.engine.runner import EngineConfig
from tickwright.observability.testing import capture_events
from tickwright.venues.hyperliquid.transport import IpRateLimited


def _flatten(
    tmp_path: Path,
    venue: HeldShort,
    frames: list[str],
    *,
    engine_config: EngineConfig | None = None,
) -> tuple[int, list[MutableMapping[str, Any]], FakeExchangeApi, FakeWsConnection]:
    """Run ``flatten`` on Hyperliquid and keep the named events it emitted."""
    with capture_events() as logs:
        exit_code, post, socket = run_flatten(
            tmp_path / "saga.db", venue, frames, engine_config=engine_config
        )
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
    # One read by cloid decides the refused close, so its verdict takes 5 seconds.
    # Only the rate limit wait can then put 10 seconds between the two closes.
    fast_verdict = EngineConfig(reconcile=ReconcileConfig(inflight_max_attempts=1))
    exit_code, logs, post, _ = _flatten(
        tmp_path, AnswersFirst(refusal), priced_frames(), engine_config=fast_verdict
    )

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


class _ReadLimitedOnce(HeldShort):
    """Refuses the first position read after the close for the IP limit, then answers."""

    def __init__(self) -> None:
        super().__init__()
        self.limited_ns: int | None = None
        self.read_after_limit_ns: int | None = None

    def account(self, payload: dict) -> dict:
        if not self.held and self.limited_ns is None:
            self.limited_ns = self.clock.timestamp_ns()
            raise IpRateLimited("429 from the venue")
        if self.limited_ns is not None and self.read_after_limit_ns is None:
            self.read_after_limit_ns = self.clock.timestamp_ns()
        return super().account(payload)


def test_a_position_read_refused_for_a_rate_limit_waits_10_s_and_is_no_dry_attempt(
    tmp_path: Path,
) -> None:
    # The IP limit covers reads too, and waiting it out makes progress (ADR-0060).
    # A dry attempt here would let a busy account give up a close that worked.
    venue = _ReadLimitedOnce()

    exit_code, logs, _, _ = _flatten(tmp_path, venue, priced_frames())

    assert exit_code == 0, [(log["event"], log.get("error")) for log in logs]
    limited = [log for log in logs if log["event"] == "flatten.rate_limited"]
    assert [(log["symbol"], log["limit"]) for log in limited] == [("HYPE", "ip")]
    assert not [log for log in logs if log["event"] == "flatten.dry_attempt"]
    assert venue.limited_ns is not None
    assert venue.read_after_limit_ns is not None
    assert venue.read_after_limit_ns - venue.limited_ns >= 10_000_000_000
    assert logs[-1]["outcome"] == "done"


REFUSED = {"status": "err", "response": "Invalid nonce"}
DEAD_SEND = ConnectionError("connection reset")


class _FilledBehindAnswer(HeldShort):
    """Gives ``answer`` for the close, yet the venue took and filled it."""

    def __init__(self, answer: object) -> None:
        super().__init__()
        self._answer = answer

    def order(self, payload: dict) -> object:
        super().order(payload)
        return self._answer

    def order_status(self, payload: dict) -> dict:
        if self.held:
            return super().order_status(payload)
        return order_status_response(
            cloid=payload["oid"],
            status="filled",
            oid=CLOSE_OID,
            coin="HYPE",
            placed_ms=START_MS + 500,
            reduce_only=True,
        )


@pytest.mark.parametrize("answer", [REFUSED, DEAD_SEND], ids=["refused", "dead_send"])
def test_a_refused_or_dead_close_the_venue_shows_filled_is_booked_and_never_sent_again(
    tmp_path: Path, answer: object
) -> None:
    # Neither answer is proof the order is absent. A dead send may have landed.
    # Flatten asks by cloid before it counts (ADR-0056). A second close beside
    # a filled one could open a position on the other side.
    venue = _FilledBehindAnswer(answer)

    exit_code, logs, post, _ = _flatten(tmp_path, venue, priced_frames())

    assert exit_code == 0, [(log["event"], log.get("error")) for log in logs]
    orders = [url for url, payload in post.requests if request_type(url, payload) == "order"]
    assert len(orders) == 1
    assert not [log for log in logs if log["event"] == "flatten.dry_attempt"]
    with SQLiteStore(tmp_path / "saga.db") as store:
        close = store.get_order(derive_cloid(f"{OPERATOR}:HYPE:1"))
    assert close is not None
    assert (close.state, close.cum_qty) == (OrderState.FILLED, Decimal("2"))
    assert logs[-1]["outcome"] == "done"


class _CloidReadLimitedOnce(_FilledBehindAnswer):
    """A dead send the venue filled. The first read by cloid is refused for the IP limit."""

    def __init__(self) -> None:
        super().__init__(DEAD_SEND)
        self.limited_ns: int | None = None
        self.read_after_limit_ns: int | None = None

    def order_status(self, payload: dict) -> dict:
        if self.limited_ns is None:
            self.limited_ns = self.clock.timestamp_ns()
            raise IpRateLimited("429 from the venue")
        if self.read_after_limit_ns is None:
            self.read_after_limit_ns = self.clock.timestamp_ns()
        return super().order_status(payload)


def test_a_cloid_read_refused_for_a_rate_limit_waits_10_s_and_is_no_dry_attempt(
    tmp_path: Path,
) -> None:
    # The verdict needs the cloid read, and waiting out the IP limit makes
    # progress (ADR-0060). A dry attempt here could give up a close that filled.
    venue = _CloidReadLimitedOnce()

    exit_code, logs, post, _ = _flatten(tmp_path, venue, priced_frames())

    assert exit_code == 0, [(log["event"], log.get("error")) for log in logs]
    limited = [log for log in logs if log["event"] == "flatten.rate_limited"]
    assert [(log["symbol"], log["limit"]) for log in limited] == [("HYPE", "ip")]
    assert not [log for log in logs if log["event"] == "flatten.dry_attempt"]
    assert venue.limited_ns is not None
    assert venue.read_after_limit_ns is not None
    assert venue.read_after_limit_ns - venue.limited_ns >= 10_000_000_000
    orders = [url for url, payload in post.requests if request_type(url, payload) == "order"]
    assert len(orders) == 1
    assert logs[-1]["outcome"] == "done"


@pytest.mark.parametrize("answer", [REFUSED, DEAD_SEND], ids=["refused", "dead_send"])
def test_a_refused_or_dead_close_the_venue_never_took_is_a_refused_dry_attempt(
    tmp_path: Path, answer: object
) -> None:
    # ADR-0060 names the reason, so the operator can tell a refusal from a thin book.
    venue = AnswersFirst(answer)

    exit_code, logs, _, _ = _flatten(tmp_path, venue, priced_frames())

    assert exit_code == 0, [(log["event"], log.get("error")) for log in logs]
    dry = [log for log in logs if log["event"] == "flatten.dry_attempt"]
    assert [(log["symbol"], log["count"], log["reason"]) for log in dry] == [("HYPE", 1, "refused")]


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
