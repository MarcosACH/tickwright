"""A leak found after the last test must fail the run (issue #572).

`filterwarnings = ["error::ResourceWarning"]` fails a test that leaks. But an
object in a reference cycle is only freed by garbage collection. When that
happens after the last test, no test is running to fail. Pytest printed the
tracebacks and exited 0, so the live suite leaked testnet sockets on every run.
"""

import os
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]

# The cycle keeps the socket alive past the test, so only the last collection
# frees it. An unconnected socket needs no network.
_LEAKS_AFTER_THE_LAST_TEST = """
import socket


def test_leaks_a_socket_in_a_cycle() -> None:
    cycle = [socket.socket()]
    cycle.append(cycle)
"""


# The interrupt stops the run after the first test, so the session fixture
# outlives it. Pytest tears that fixture down in its own `pytest_sessionfinish`,
# and only then is the cycle garbage. A failed assertion would not do here.
# Pytest keeps its traceback in `sys.last_exc`, which holds the cycle alive.
_LEAKS_IN_THE_FINAL_TEARDOWN = """
import socket

import pytest


@pytest.fixture(scope="session")
def leaky():
    cycle = [socket.socket()]
    cycle.append(cycle)
    yield cycle


@pytest.mark.usefixtures("leaky")
def test_interrupts_the_run() -> None:
    raise KeyboardInterrupt


@pytest.mark.usefixtures("leaky")
def test_never_runs() -> None:
    pass
"""


def _run_pytest(tmp_path: Path, source: str, *args: str) -> str:
    test_file = tmp_path / "test_leaks.py"
    test_file.write_text(source)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(test_file),
            # The project's config, so the run loads the same plugins as ours.
            "-c",
            str(_REPO / "pyproject.toml"),
            "--rootdir",
            str(tmp_path),
            "-p",
            "no:cacheprovider",
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=20,
        env={k: v for k, v in os.environ.items() if not k.startswith("TICKWRIGHT_")},
    )
    return f"exit={result.returncode}\n{result.stdout}{result.stderr}"


def test_a_leak_found_after_the_last_test_fails_the_run(tmp_path: Path) -> None:
    output = _run_pytest(tmp_path, _LEAKS_AFTER_THE_LAST_TEST)

    # The test itself passes. If the socket were freed inside it, this run
    # would prove the old guard, not the new one.
    assert "1 passed" in output
    assert "exit=0" not in output
    assert "unclosed <socket.socket" in output


def test_a_leak_freed_in_the_final_teardown_is_reported(tmp_path: Path) -> None:
    output = _run_pytest(tmp_path, _LEAKS_IN_THE_FINAL_TEARDOWN)

    assert "KeyboardInterrupt" in output
    assert "resource leaks found after the last test" in output
    assert "unclosed <socket.socket" in output
