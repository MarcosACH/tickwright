"""Structured-logging configuration — the one place the processor chain is wired.

``configure_logging`` installs the ADR-0020 chain: ambient correlation ids merged
in, a level and ISO timestamp added, secrets redacted, then rendered — JSON for
aggregation or a console renderer for a human at a terminal. The composition root
calls it once at start-up with the run's secrets; tests call it with a
``StringIO`` stream to assert on exactly what an aggregator would see.

Logger caching is off so a reconfiguration (a test's, or a re-init) takes effect
immediately; latency is an explicit non-goal, so the lost cache costs nothing.
"""

import sys
from collections.abc import Iterable
from typing import TextIO

import structlog
from structlog.typing import Processor

from .redaction import redact, register_secrets

# The record-shaping processors — correlation-id merge, then redaction — that
# decide what *content* a consumer sees, as opposed to the presentation
# processors (level, timestamp, renderer) that only format it. Named once here
# and shared verbatim with the test seam (``capture_events``), so a test
# observes exactly the fields a real record carries and a new content rule (a
# second redaction pass, a field sampler) lands in both paths by construction
# rather than drifting. Redaction runs ahead of the level/timestamp adders,
# which is output-identical: neither a fixed level string nor a generated ISO
# timestamp can carry a secret, so scrubbing before they are added scrubs the
# same content.
RECORD_PROCESSORS: tuple[Processor, ...] = (
    structlog.contextvars.merge_contextvars,
    redact,
)


def build_processors(*, json_output: bool) -> list[Processor]:
    """The full production chain: the shared record processors, then the level
    and ISO-timestamp adders, then the renderer — JSON for aggregation, the
    console renderer for a human at a terminal.

    Pure: touches no global state (unlike ``configure_logging``), so a test can
    assert on the chain's shape — notably that it opens with ``RECORD_PROCESSORS``.
    """
    renderer: Processor = (
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    )
    return [
        *RECORD_PROCESSORS,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        renderer,
    ]


def configure_logging(
    *,
    stream: TextIO | None = None,
    json_output: bool = True,
    secrets: Iterable[str] = (),
) -> None:
    """Wire the structlog processor chain to ``stream`` (stderr by default).

    ``secrets`` are the exact strings to redact from every record (the signing
    key, notably). ``json_output`` selects JSON (aggregation) over the console
    renderer (local development).
    """
    register_secrets(secrets)
    structlog.configure(
        processors=build_processors(json_output=json_output),
        logger_factory=structlog.PrintLoggerFactory(file=stream or sys.stderr),
        cache_logger_on_first_use=False,
    )
