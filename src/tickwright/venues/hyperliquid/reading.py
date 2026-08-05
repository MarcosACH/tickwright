"""The venue read vocabulary — how one reported value becomes an engine value,
and what "unreadable" means when it does not (ADR-0011 inv 1).

Three grains of this venue parse reported figures — ``clearinghouseState`` at the
account grain, ``userFills`` at the fill grain, the ``trades`` channel at the tick
grain — and until #218 each carried its own idea of what a figure may be. The
account grain froze on a re-typed one while the other two coerced it through, so
the same venue answered the same question two ways depending on which endpoint
asked. This module is the one answer.

It sits in the venue package rather than ``domain`` because the load-bearing fact
— *this venue reports every figure as a decimal string* — is Hyperliquid's
contract, not a universal one; a second venue may well report JSON numbers, and
ADR-0031 keeps venue knowledge in the venue. What **is** universal is that a
figure must be a number at all, and that lives in ``domain.exact_figure``, which
this delegates to. Same discipline as ``ingress.py``: a second venue is the
signal to promote something here to a shared home, not before.
"""

from decimal import Decimal

from tickwright.domain import exact_figure

UNREADABLE = (ArithmeticError, KeyError, TypeError, ValueError)
"""What reading a reported body raises when it cannot be read.

Every grain answers an unreadable body the same way — a dropped frame on the
feed, a named ``None`` on a venue read — so every grain catches the same four,
and naming them once is what keeps that true. They had already drifted: the tick
grain caught ``InvalidOperation`` where the other two caught its ``ArithmeticError``
superset, which is a difference no one intended and nothing would have surfaced.

* ``KeyError`` — a field the shape check did not cover is absent.
* ``TypeError`` — a value is the wrong type: a re-typed figure, or a row that is
  not a mapping at all.
* ``ValueError`` — a value is the right type and still unreadable: a non-finite
  figure (``exact_figure``), or a margin mode outside the two the venue reports.
* ``ArithmeticError`` — how ``decimal`` signals. ``Decimal("nope")`` raises
  ``InvalidOperation``, which is **not** a ``ValueError``, so this branch is what
  catches an unparseable numeric string.

Deliberately a tuple and not a new exception type: a body we cannot read is a
failed read, and every caller already has a guard that says what that means at
its own layer. This names control flow that exists; it does not add any.

What is deliberately **outside** it is ``VenueFactUnsupported`` (ADR-0048): the
venue reporting a fact we understand and cannot represent — a fill fee settled in
a token other than USDC. Every member above describes a body we could not parse,
where a re-read may succeed, so a named ``None`` the cycle retries is the honest
verdict. A settled venue row never changes, so that verdict would freeze the
reconciler on it forever; it escalates past every guard here instead, and being a
type no caller catches is what keeps that true by construction.
"""


def figure(reported: object) -> Decimal:
    """One venue figure as an exact number, or a failed read if it is not one.

    Every quantity this venue reports goes through here, for the two unreadable
    values that do not announce themselves.

    **Not a string.** The venue reports every one of these figures as a decimal
    string — first-party ``WsTrade`` is ``px: string, sz: string``, the
    ``userFills`` ``Fill`` the same, and ``clearinghouseState`` likewise — so a
    JSON *number* is the venue changing its contract: the same "we are not
    reading what we think we are" a missing field means, and frozen for the same
    reason.

    It cannot be waved through as equivalent either, and the reason is one layer
    earlier than it looks. The loss is not in *our* coercion — ``Decimal(str(x))``
    goes through the shortest round-tripping repr, so a re-typed ``0.002`` would
    still land as an exact ``Decimal("0.002")``. It is in ``json.loads``: a JSON
    number is a ``float`` before this function is ever reached, so any digit the
    venue reported beyond what a double holds is *already gone* — a reported
    ``43250.123456789012345`` arrives as ``43250.12345678901`` — and the reported
    scale is gone with it, ``0.10`` and ``0.1`` being the same double. Neither is
    recoverable downstream, and both are durable once ``_records.py`` round-trips
    the figure. A number we cannot prove is the one the venue sent is not the
    exact figure ADR-0029 builds every price on. Taking ``object`` rather than
    ``Any`` is what makes the check load-bearing — the type checker will not let
    a caller reach ``Decimal`` around it.

    *(The pinned ``hyperliquid-python-sdk`` 0.24.0 disagrees at the tick grain:
    its ``Trade`` TypedDict declares ``sz: int`` and omits ``tid``/``users``
    entirely. It is stale — the first-party ``WsTrade`` interface carries both
    fields our reader depends on, and our recorded frames match it. The docs win;
    this note exists so the next reader does not re-derive that from the SDK and
    conclude the guard is wrong.)*

    **Not finite.** Deferred to ``exact_figure``: ``Decimal("nan")`` and
    ``Decimal("Infinity")`` are *valid* constructions, so a non-finite figure is
    the one unreadable value that raises nothing on the way in, and the argument
    for refusing it does not vary by venue.

    What each refusal *means* is the caller's own: a tick drops its row, a fills
    read is a named ``None``, an account read freezes the reconcile. This raises
    into ``UNREADABLE``, which every one of them already catches, so it adds a
    check and never a control flow.
    """
    if not isinstance(reported, str):
        raise TypeError(f"non-string figure {reported!r}")
    return exact_figure(Decimal(reported))
