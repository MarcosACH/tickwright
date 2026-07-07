"""Named lifecycle events — first-class, test-assertable telemetry (ADR-0020).

A named event is a stable, documented name emitted as a structured record with
an ``event`` field — distinct from free-text logs. The catalog (``NamedEvent``)
is a contract: a state-affecting path with no named event is a defect, and a name
outside the catalog raises rather than emitting a silent record. Emission rides
structlog, so records are structured in production and capturable in tests via
``structlog.testing.capture_logs``.

Correlation ids (a per-process run id, a per-operation ``cloid``/``signal_id``/
reconcile ``cycle``) are ambient ``ContextVar``s auto-injected into every record
by the processor chain — never passed as event fields.
"""

import structlog

from .catalog import NamedEvent

_log = structlog.get_logger("tickwright")

_CATALOG = frozenset(NamedEvent)


def named_event(name: NamedEvent | str, /, **fields: object) -> None:
    """Emit the cataloged lifecycle event ``name`` with structured ``fields``.

    ``name`` must be a ``NamedEvent`` (or a string equal to one). An uncataloged
    name raises ``ValueError`` — the runtime half of "a state-affecting path with
    no named event is a defect": a typo or an undocumented name never ships as a
    silent record. Correlation ids are not passed here; they ride the ambient
    context and are merged in by the processor chain.
    """
    if name not in _CATALOG:
        raise ValueError(
            f"uncataloged named event {name!r}: add it to NamedEvent (ADR-0020) before emitting it"
        )
    _log.info(str(name), **fields)


__all__ = ["NamedEvent", "named_event"]
