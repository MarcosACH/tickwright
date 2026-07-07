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

from .redaction import redact, register_secrets


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
    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact,
            renderer,
        ],
        logger_factory=structlog.PrintLoggerFactory(file=stream or sys.stderr),
        cache_logger_on_first_use=False,
    )
