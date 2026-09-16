"""Named lifecycle events — first-class, test-assertable telemetry (ADR-0020).

A named event is a stable, documented name emitted as a structured record with
an ``event`` field — distinct from free-text logs. The catalog (``NamedEvent``)
is a contract: a state-affecting path with no named event is a defect, and a name
outside the catalog raises rather than emitting a silent record. Each name
declares its field set (``FIELDS``, #338), and a call whose fields differ raises
the same way. Emission rides structlog, so records are structured in production
and capturable in tests via ``structlog.testing.capture_logs``.

Correlation ids (a per-process run id, a per-operation ``cloid``/``signal_id``/
reconcile ``cycle``) are ambient ``ContextVar``s auto-injected into every record
by the processor chain. The engine never passes them as event fields. A venue
adapter sits below the seam and cannot know which scope its caller bound, so
where its record is about one order it names that ``cloid`` as a field of its
own.
"""

import structlog

from .catalog import FIELDS, NamedEvent

_log = structlog.get_logger("tickwright")

_CATALOG = frozenset(NamedEvent)


def named_event(name: NamedEvent | str, /, **fields: object) -> None:
    """Emit the cataloged lifecycle event ``name`` with structured ``fields``.

    ``name`` must be a ``NamedEvent`` (or a string equal to one). An uncataloged
    name raises ``ValueError`` — the runtime half of "a state-affecting path with
    no named event is a defect": a typo or an undocumented name never ships as a
    silent record. A call whose fields differ from the set ``FIELDS`` declares
    for ``name`` raises the same way (#338). Correlation ids are not passed here;
    they ride the ambient context and are merged in by the processor chain.
    """
    if name not in _CATALOG:
        raise ValueError(
            f"uncataloged named event {name!r}: add it to NamedEvent (ADR-0020) before emitting it"
        )
    # Every member is declared (``test_catalog``), so a missing entry is a
    # defect and never a reason to emit unchecked. It is named like the other
    # two refusals, so the message says what to change.
    try:
        declared = FIELDS[NamedEvent(name)]
    except KeyError as exc:
        raise ValueError(
            f"named event {name!r} has no entry in FIELDS: declare its field set (ADR-0020)"
        ) from exc
    if set(fields) != declared:
        raise ValueError(
            f"named event {name!r} declares fields {sorted(declared)} "
            f"but was given {sorted(fields)}: change FIELDS (ADR-0020) or the call site"
        )
    _log.info(str(name), **fields)


__all__ = ["NamedEvent", "named_event"]
