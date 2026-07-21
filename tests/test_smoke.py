"""Smoke test: the package imports and exposes a version.

Keeps the suite non-empty from the first commit and proves the src-layout
package is installed and importable in the uv environment.
"""

import importlib
import importlib.metadata
from collections.abc import Iterator
from importlib.metadata import PackageNotFoundError
from unittest import mock

import pytest

import tickwright


@pytest.fixture
def restore_tickwright_version() -> Iterator[None]:
    """Reload ``tickwright`` after a test patches its import-time metadata lookup.

    ``__version__`` is computed at import from installed metadata; a test that
    patches that lookup and reloads the module leaves a bogus ``__version__``
    behind. Reloading in teardown — after the test's patch context has exited —
    restores the real version so later tests and modules stay honest.
    """
    yield
    importlib.reload(tickwright)


def test_package_exposes_version() -> None:
    assert isinstance(tickwright.__version__, str)
    assert tickwright.__version__


def test_version_tracks_installed_metadata() -> None:
    assert tickwright.__version__ == importlib.metadata.version("tickwright")


def test_version_reads_from_installed_metadata(restore_tickwright_version: None) -> None:
    def fake_version(name: str) -> str:
        assert name == "tickwright"
        return "9.9.9-sentinel"

    with mock.patch.object(importlib.metadata, "version", fake_version):
        importlib.reload(tickwright)
        assert tickwright.__version__ == "9.9.9-sentinel"


def test_version_falls_back_when_not_installed(restore_tickwright_version: None) -> None:
    with mock.patch.object(importlib.metadata, "version", side_effect=PackageNotFoundError):
        importlib.reload(tickwright)
        assert tickwright.__version__ == "0.0.0+unknown"
