"""Isolate ambient and global logging state between observability tests.

``bind_run_id``/``operation`` write to process-wide ``contextvars`` and
``configure_logging`` mutates the global structlog config and secret registry —
none of it may leak into the next test. Clearing and resetting around each test
keeps every assertion about "what rides a record" self-contained.
"""

from collections.abc import Generator

import pytest
import structlog
from structlog.contextvars import clear_contextvars

from tickwright.observability.redaction import register_secrets


@pytest.fixture(autouse=True)
def _isolate_observability() -> Generator[None]:
    clear_contextvars()
    yield
    clear_contextvars()
    register_secrets(())
    structlog.reset_defaults()
