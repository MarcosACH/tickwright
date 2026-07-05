"""Named lifecycle events — first-class, test-assertable telemetry (ADR-0020).

A named event is a stable, documented name emitted as a structured record with
an ``event`` field — distinct from free-text logs. The catalog is a contract:
a state-affecting path with no named event is a defect. Emission rides
structlog, so records are structured in production and capturable in tests via
``structlog.testing.capture_logs``.
"""

import structlog

_log = structlog.get_logger("tickwright")


def named_event(name: str, /, **fields: object) -> None:
    """Emit the named lifecycle event ``name`` with structured ``fields``."""
    _log.info(name, **fields)
