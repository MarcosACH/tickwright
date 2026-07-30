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

    Letting one through is fail-*open* in the worst direction available, because
    a ``NaN`` does not propagate as an error — it propagates as **agreement**.
    Every comparison against it is false, so a ``NaN`` price passes any band,
    slippage or min-notional check it is measured against rather than failing it,
    and a ``NaN`` quantity makes ``cum_qty`` arithmetic absorb a fill silently.
    An infinity is the mirror: it wins every comparison and would drive an
    unbounded action. Either is durable once written — the store round-trips
    ``"NaN"`` back into a ``Decimal("NaN")`` — which is why the boundary is the
    only place to stop it.

    ``ValueError`` and not a new exception type: a figure that is not a number is
    an unreadable response, and every caller already has a guard that says what
    that means at its own layer (a dropped frame, a named failed read). This adds
    a check, never a control flow.
    """
    if not reported.is_finite():
        raise ValueError(f"non-finite figure {reported!r}")
    return reported
