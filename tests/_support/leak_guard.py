"""Fail the run on a resource leak found after the last test (issue #572).

`filterwarnings = ["error::ResourceWarning"]` only fails the test that is
running when the leak is freed. An object in a reference cycle waits for
garbage collection, which can come after the last test. Pytest then reports
it as a warning and still exits 0.
"""

import gc
import warnings

import pytest


# Last, so pytest's own final fixture teardown has already run. An interrupted
# run tears its session fixtures down there, and a leak freed then counts too.
@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session) -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        gc.collect()
    leaks = [w for w in caught if issubclass(w.category, ResourceWarning)]
    if not leaks:
        return

    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_sep("=", "resource leaks found after the last test", red=True)
        for leak in leaks:
            reporter.write_line(str(leak.message))
    if session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
