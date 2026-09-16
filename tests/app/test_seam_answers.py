"""The gate over whether the seam gates were answered (#303).

Every other caller of ``seam_answers`` is the real tree. The ``tmp_path``
cases here are the ones that prove the assertion bites: a suite that answers
nothing must fail, or the gate ships with the defect it exists to close.
"""

from pathlib import Path

import pytest
from seam_answers import Answered, assert_every_adapter_answers_its_gates

from tickwright.domain import MarketFeed


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
            configured=["fake"],
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
