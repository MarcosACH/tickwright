"""The one guard against a figure that is not a number (ADR-0011 inv 1).

Every quantity in this engine is an exact ``Decimal``, parsed at a boundary from
a figure someone else reported — a venue body, a replay file. ``Decimal`` accepts
two values that are not numbers at all, and this is where they are refused.

It lives in ``domain`` because both ``venues`` and ``adapters`` parse such
figures and neither may import the other (ADR-0032); it stays a bare check over
an already-built ``Decimal`` rather than a parser, because *what a figure is
encoded as* is a boundary's own business — the venue reports decimal strings and
freezes on a JSON number (``venues/hyperliquid/account.py``), a replay file is
looser — while *what qualifies as a quantity* is the same everywhere.
"""

from decimal import Decimal


def exact_figure(reported: Decimal) -> Decimal:
    """``reported`` back if it is an exact number; ``ValueError`` if it is not.

    ``Decimal("nan")`` and ``Decimal("Infinity")`` are **valid** constructions,
    so a non-finite figure is the one unreadable value that announces nothing: it
    parses without raising and rides into a domain quantity, where a missing
    field or a non-numeric string would have frozen the read.

    Letting one through fails open, though not the way the ``float`` intuition
    says. ``Decimal`` is stricter than IEEE-754 here: an *ordering* comparison
    against a ``NaN`` raises ``InvalidOperation`` rather than returning false
    (measured — ``<``, ``>``, ``min``, ``sorted`` all signal). So a ``NaN`` price
    does not quietly clear a band check; it detonates one, at an arbitrary later
    point deep in the engine, where the surrounding guard is ``except OSError``
    or nothing and the fault is attributed to the wrong layer.

    What *is* silent is everything else. ``nan == nan`` is false, so a ``cum_qty``
    cross-check never agrees and an order never reaches FILLED, while a reconcile
    equality reads permanent divergence and heals toward a figure no venue holds.
    Arithmetic propagates it: one ``NaN`` fill poisons the running total and
    every figure derived from it. And it is **durable** — the store round-trips
    ``"NaN"`` back into a ``Decimal("NaN")`` on recovery, so a single bad read
    outlives the process that took it.

    An infinity is the mirror and needs no subtlety: it compares greater than
    anything, so it clears whatever ceiling it meets and would drive an unbounded
    action. Which is why the boundary is the only place to stop either.

    ``ValueError`` and not a new exception type: a figure that is not a number is
    an unreadable response, and every caller already has a guard that says what
    that means at its own layer (a dropped frame, a named failed read). This adds
    a check, never a control flow.
    """
    if not reported.is_finite():
        raise ValueError(f"non-finite figure {reported!r}")
    return reported
