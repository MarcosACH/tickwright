"""One error contract for the whole ``Store`` seam (ADR-0019).

A store member either reaches durable storage or raises ``InvariantViolation``.
That is a property of the *seam*, not of whichever method the caller happened to
touch: the engine's containment net keys on the exception's type — only an
``InvariantViolation`` pierces it and faults the run (ADR-0024) — so a driver
exception crossing the seam is filed as a caller's bug and survived, when the
truth is that the process can no longer make its state durable.

Reads are covered as well as writes, which widens ADR-0014's "failed checkpoint
write": one contract means a store call can move under the containment net later
without its failure mode changing shape.

Each adapter contributes exactly one thing to that rule: the base class its
driver raises from. Both ``sqlite3`` and ``psycopg`` expose one (`sqlite3.Error`,
`psycopg.Error`), so the translation itself has no per-backend half and lives
here.

Deliberately **not** applied to ``close()``: teardown that fails is a leaked
resource, which the runner already records as a stop-hook failure, not a claim
about durability that turned out false.
"""

from collections.abc import Callable
from functools import wraps
from typing import Concatenate, Protocol

from tickwright.domain import InvariantViolation


class _Adapter(Protocol):
    """A store adapter, which knows the exception base its driver raises from.

    Read-only by construction: a plain class attribute satisfies it, and a
    property member keeps the type covariant, so ``type[sqlite3.Error]`` is an
    acceptable ``type[Exception]``.
    """

    @property
    def _driver_error(self) -> type[Exception]: ...


def durable[S: _Adapter, **P, R](
    method: Callable[Concatenate[S, P], R],
) -> Callable[Concatenate[S, P], R]:
    """Translate this member's driver failures into ``InvariantViolation``.

    Wraps the whole body rather than any single statement, so a transaction that
    fails on *commit* is caught alongside one that fails on a write — by then the
    backend has already rolled back, and what the caller must not do is run on
    believing the write landed.

    The original is chained (``from exc``): the driver's message is the only
    thing that says *why*, and losing it would trade one unusable diagnostic for
    another.
    """

    @wraps(method)
    def guarded(adapter: S, /, *args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return method(adapter, *args, **kwargs)
        except adapter._driver_error as exc:
            raise InvariantViolation(f"store {method.__name__} refused: {exc}") from exc

    return guarded
