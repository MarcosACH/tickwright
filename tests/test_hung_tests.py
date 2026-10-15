"""A hung test must end the run, not run for hours (issue #510).

The engine tests drive a ``ManualClock``, so every wait ends at once. A loop that
never stops then spins at full speed, and ``capture_events`` keeps every log line
it writes. On 2026-10-15 one such test grew to 77 GB and ran for over 2 hours.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]

_SPINS = """
import asyncio


def test_spins_forever() -> None:
    async def spin() -> None:
        while True:
            await asyncio.sleep(0)

    asyncio.run(spin())
"""


def test_every_test_runs_under_a_timeout(pytestconfig: pytest.Config) -> None:
    assert float(pytestconfig.getini("timeout")) > 0


def test_a_spinning_asyncio_test_is_stopped_at_its_timeout(tmp_path: Path) -> None:
    test_file = tmp_path / "test_spins.py"
    test_file.write_text(_SPINS)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(test_file),
            # The project's config, so the run uses its timeout method. Only the
            # length is cut short, so this test does not wait the full default.
            "-c",
            str(_REPO / "pyproject.toml"),
            "--rootdir",
            str(tmp_path),
            "-o",
            "timeout=1",
            "-p",
            "no:cacheprovider",
        ],
        capture_output=True,
        text=True,
        # A run with no working timeout spins until this kills it.
        timeout=60,
        env={k: v for k, v in os.environ.items() if not k.startswith("TICKWRIGHT_")},
    )

    assert result.returncode != 0
    assert "Timeout" in result.stdout + result.stderr
