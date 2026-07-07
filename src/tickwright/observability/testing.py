"""Test seam for asserting on named events *with* their ambient correlation ids.

``structlog.testing.capture_logs`` disables the whole processor chain, so bound
``contextvars`` (run id, cloid, cycle, …) never reach the captured record.
``capture_events`` re-runs the production ``RECORD_PROCESSORS`` (correlation
merge, then redaction) ahead of capture, so a test observes exactly what a real
log line would carry — the merged run/operation ids and any redaction applied.
Sharing that one segment with ``configure_logging`` is what keeps the test seam
a true mirror of production rather than a hand-maintained approximation.
"""

from collections.abc import Generator
from contextlib import contextmanager

from structlog.testing import capture_logs
from structlog.typing import EventDict

from .logging import RECORD_PROCESSORS


@contextmanager
def capture_events() -> Generator[list[EventDict]]:
    """Capture named events with the production record-shaping processors
    applied — unlike ``capture_logs``, whose captured records omit the ambient
    ids and leave secrets unredacted."""
    with capture_logs(processors=list(RECORD_PROCESSORS)) as entries:
        yield entries
