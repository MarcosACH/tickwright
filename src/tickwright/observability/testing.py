"""Test seam for asserting on named events *with* their ambient correlation ids.

``structlog.testing.capture_logs`` disables the whole processor chain, so bound
``contextvars`` (run id, cloid, cycle, …) never reach the captured record.
``capture_events`` re-runs the correlation-merge (and redaction) processors ahead
of capture, so a test observes exactly what a real log line would carry — the
merged run/operation ids and any redaction applied.
"""

from collections.abc import Generator
from contextlib import contextmanager

from structlog.contextvars import merge_contextvars
from structlog.testing import capture_logs
from structlog.typing import EventDict

from .redaction import redact


@contextmanager
def capture_events() -> Generator[list[EventDict]]:
    """Capture named events with the correlation-merge and redaction processors
    applied — unlike ``capture_logs``, whose captured records omit the ambient
    ids and leave secrets unredacted."""
    with capture_logs(processors=[merge_contextvars, redact]) as entries:
        yield entries
