"""The gate over whether the seam gates were answered (#303).

Two halves. The ``tmp_path`` cases prove the assertion bites: a suite that
answers nothing must fail, or the gate ships with the defect it exists to
close. The two real-tree tests at the end are the gate itself, over every
adapter ``AppConfig`` can select. The map beside them is the one line a venue
author adds here, and the gate names every answer still owed until the
suite answers it.
"""

from pathlib import Path
from typing import Literal

import pytest
from seam_answers import Answered, assert_every_adapter_answers_its_gates

from tickwright.app import AppConfig
from tickwright.domain import Exchange, MarketFeed

TESTS = Path(__file__).parent.parent

FEEDS = {
    "replay": Answered("ReplayFeed", suite=TESTS / "adapters" / "feed"),
    "hyperliquid": Answered("HyperliquidFeed", suite=TESTS / "venues" / "hyperliquid"),
}
EXCHANGES = {
    "paper": Answered("PaperExchange", suite=TESTS / "adapters" / "paper"),
    "hyperliquid": Answered("HyperliquidExchange", suite=TESTS / "venues" / "hyperliquid"),
}


def _configured(field: str) -> object:
    """``AppConfig.<field>``'s ``Literal``, read off the model."""
    return AppConfig.model_fields[field].annotation


def test_every_configurable_feed_answers_every_gate_a_feed_owes() -> None:
    assert_every_adapter_answers_its_gates(
        MarketFeed, configured=_configured("feed"), answers=FEEDS
    )


def test_every_configurable_exchange_answers_every_gate_an_exchange_owes() -> None:
    assert_every_adapter_answers_its_gates(
        Exchange, configured=_configured("exchange"), answers=EXCHANGES
    )


def _suite(root: Path, body: str) -> Path:
    """One test module in ``root``, shaped like an adapter suite."""
    (root / "test_fake.py").write_text(body, encoding="utf-8")
    return root


def test_a_suite_that_answers_no_gate_fails_naming_the_adapter_and_every_gate(
    tmp_path: Path,
) -> None:
    """The red case the ticket asks for, as a fixture rather than an assertion.

    A feed owes three answers. A suite with a test of its own and no call to
    any of the three is green under pytest alone, and this is what has to go
    red instead, naming what it never answered so the author knows what to add.
    """
    suite = _suite(tmp_path, "def test_something_of_its_own() -> None:\n    pass\n")

    with pytest.raises(AssertionError) as failure:
        assert_every_adapter_answers_its_gates(
            MarketFeed,
            configured=Literal["fake"],
            answers={"fake": Answered("FakeFeed", suite=suite)},
        )

    message = str(failure.value)
    assert "FakeFeed" in message
    for gate in (
        "assert_every_member_is_claimed",
        "assert_every_traded_symbol_is_marked",
        "assert_quiet_once_stopped",
    ):
        assert gate in message


_ANSWERS_ALL_THREE = """
from pathlib import Path

from tickwright.domain import {protocol}


def test_claims() -> None:
    assert_every_member_is_claimed({protocol}, _SEAM_CLAIMS, suite=Path(__file__).parent)


def test_marked() -> None:
    assert_every_traded_symbol_is_marked(transcript, feed="{feed}")


def test_quiet() -> None:
    await_quiet = assert_quiet_once_stopped(transcript, adapter=feed, task=task, name="{feed}")
"""


def test_a_suite_answering_every_gate_under_the_adapter_s_own_name_is_green(
    tmp_path: Path,
) -> None:
    """The shape both shipped feed suites have, in miniature."""
    suite = _suite(tmp_path, _ANSWERS_ALL_THREE.format(protocol="MarketFeed", feed="FakeFeed"))

    assert_every_adapter_answers_its_gates(
        MarketFeed, configured=Literal["fake"], answers={"fake": Answered("FakeFeed", suite=suite)}
    )


def test_a_gate_answered_under_another_name_does_not_count(tmp_path: Path) -> None:
    """The answer is keyed on the adapter, not on the call being present.

    ``tests/adapters/feed/test_feed_contract.py`` drives the mark gate with a
    fake ``TradesOnlyFeed`` to prove the gate bites. A read that counted any
    call would count that one for every feed. And a claims call over the other
    seam's Protocol is a claims call for the other seam.
    """
    suite = _suite(tmp_path, _ANSWERS_ALL_THREE.format(protocol="Exchange", feed="OtherFeed"))

    with pytest.raises(AssertionError) as failure:
        assert_every_adapter_answers_its_gates(
            MarketFeed,
            configured=Literal["fake"],
            answers={"fake": Answered("FakeFeed", suite=suite)},
        )

    message = str(failure.value)
    for gate in (
        "assert_every_member_is_claimed",
        "assert_every_traded_symbol_is_marked",
        "assert_quiet_once_stopped",
    ):
        assert gate in message


def test_a_map_that_misses_a_configured_adapter_fails_before_any_suite_is_read() -> None:
    """The stale-map failure the ``seam_claims`` guard exists for, one level up.

    A venue added to the ``Literal`` and not to the map is the exact shape of
    the defect: selectable by ``build_*``, owing every answer, and named
    nowhere the gate would look. It must fail on the value, not on a
    ``KeyError`` nobody wrote a message for.
    """
    with pytest.raises(AssertionError, match=r"'unmapped'"):
        assert_every_adapter_answers_its_gates(
            MarketFeed, configured=Literal["unmapped"], answers={}
        )


def test_a_map_entry_for_an_adapter_no_longer_configured_fails(tmp_path: Path) -> None:
    """The other direction: a venue removed from the ``Literal`` leaves a stale row.

    Left in place it would keep asserting answers for a suite that may be gone,
    and a directory that no longer exists reads as one that answers nothing.
    """
    with pytest.raises(AssertionError, match=r"'gone'"):
        assert_every_adapter_answers_its_gates(
            MarketFeed,
            configured=Literal["kept"],
            answers={
                "kept": Answered("KeptFeed", suite=tmp_path),
                "gone": Answered("GoneFeed", suite=tmp_path),
            },
        )
