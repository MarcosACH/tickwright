"""Ambient correlation ids (ADR-0020).

Two scopes, both carried in ``contextvars`` and merged into every record by
``structlog.contextvars.merge_contextvars`` in the processor chain:

* a **run id** bound once per process (``bind_run_id``), and
* a **per-operation id** bound for the span of one unit of work (``operation``) —
  the ``cloid`` while a saga transition is handled, the ``signal_id`` while a
  signal is handled, the reconcile ``cycle`` while a cycle runs.

Because they are ambient, no call site repeats them as event fields; the merge
processor injects them and clears them at scope exit. ``operation`` nests, so a
per-order ``cloid`` and the enclosing ``cycle`` both ride a reconcile record.
"""

from contextlib import AbstractContextManager

from structlog.contextvars import bind_contextvars, bound_contextvars


def bind_run_id(run_id: str) -> None:
    """Bind the per-process ``run_id`` for the rest of this context's lifetime.

    Set once at engine start; every record from then on is traceable to the run.
    """
    bind_contextvars(run_id=run_id)


def operation(**ids: str) -> AbstractContextManager[None]:
    """Bind per-operation correlation ``ids`` for the duration of the ``with`` block.

    Restores the prior binding on exit, so an id never leaks past its unit of
    work. Nests cleanly — an inner ``operation(cloid=...)`` adds to an outer
    ``operation(cycle=...)`` rather than replacing it.
    """
    return bound_contextvars(**ids)
