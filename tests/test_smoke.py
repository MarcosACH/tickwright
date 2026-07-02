"""Smoke test: the package imports and exposes a version.

Keeps the suite non-empty from the first commit and proves the src-layout
package is installed and importable in the uv environment.
"""

import tickwright


def test_package_exposes_version() -> None:
    assert isinstance(tickwright.__version__, str)
    assert tickwright.__version__
