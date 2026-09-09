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

from collections.abc import Awaitable, Callable, Mapping
from decimal import Decimal
from typing import Any, NoReturn

from tickwright.domain import VenueFactUnsupported, VenueReadFailure, exact_figure
from tickwright.observability import NamedEvent, named_event

UNREADABLE = (ArithmeticError, KeyError, TypeError, ValueError)
"""What reading a reported body raises when it cannot be read.

Every grain answers an unreadable body the same way — a dropped frame on the
feed, a named ``VenueReadFailure`` on a venue read — so every grain catches the
same four, and naming them once is what keeps that true. They had already
drifted: the tick grain caught ``InvalidOperation`` where the other two caught
its ``ArithmeticError`` superset, which is a difference no one intended and
nothing would have surfaced.

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
where a re-read may succeed, so a named ``VenueReadFailure`` the cycle retries is
the honest verdict. A settled venue row never changes, so this one is already
known permanent at the *first* read: answering it that way would spend the whole
unreadable span re-reading it to the same refusal and then fault on a cloid,
naming strictly less than the fact does (ADR-0049 §4). It escalates past every
guard here instead, and being a type no caller catches is what keeps that true by
construction.
"""


async def read[T](
    *,
    request: str,
    query: dict[str, Any],
    send: Callable[[dict[str, Any]], Awaitable[object]],
    normalize: Callable[[object], T],
    cloid: str | None = None,
) -> T | VenueReadFailure:
    """One in-flight venue read: send ``query``, and answer the three ways it
    can come back other than an answer.

    A ``VenueReadFailure`` means the read **failed** — never "the venue said
    nothing" (ADR-0011 inv 1). Two disjoint causes reach it, and both are named
    before it is returned, because a freeze an operator cannot attribute is the
    quiet outage that guard exists to prevent:

    - the **send** failed (``OSError``, ``TimeoutError`` among it) — no body
      arrived, so there is nothing to parse: ``SEND_FAILED``;
    - the **body** arrived and could not be read (``UNREADABLE``) — a shape
      outside the venue's contract, a missing field, a figure that is not one:
      ``UNREADABLE_BODY``.

    Both are transient by assumption: the caller retries at its next deadline
    and nothing was guessed in the meantime. They are **two members and not
    one** because a caller reading a *worklist* needs to know whether the venue
    answered at all before deciding how many of its other orders one failure
    should cost (ADR-0049); a caller reading a single grain ignores the
    distinction and collapses both. What deliberately does *not* stop here is
    ``VenueFactUnsupported`` — a fact the venue reported and this engine cannot
    represent, which a retry re-reads identically forever, so it escalates past
    every guard here rather than being answered as something time could fix
    (ADR-0048).

    Splitting the two ``try`` blocks is what lets the unreadable branch quote the
    body it could not read; splitting them *this* way is what keeps ``normalize``
    honest, since a normalizer that raised ``OSError`` would otherwise be filed
    as a dead connection.

    Why the read and not just the vocabulary lives here: before this, "one venue
    read" was spread across three collaborators — a sender, a per-caller
    ``try``/``except`` deciding what a transport failure meant, and a normalizer
    deciding what an unreadable body meant — so **which layer owned which
    failure was chosen freshly at every read site**, and the sites disagreed. One
    returned ``None`` and named it, one returned ``None`` silently, one raised.
    Owning the taxonomy once is what makes a new read inherit it instead of
    re-deriving it (issue #216).

    What decides whether a read belongs here is **whether something above it
    retries**, not whether it runs at boot. A ``VenueReadFailure`` is a freeze
    signal, and a freeze only means anything to a caller that will ask again:

    - ``fetch_account_state`` is a boot read *and* a caller. It runs as a
      ``StartupBarrier`` step, and the barrier re-drives its whole sequence with
      capped backoff until the startup budget is spent — so ``None`` is exactly
      the "could not prove it, guessed nothing" the barrier is written to take.
    - ``universe.py`` runs at composition, before anything that could re-drive
      it, and the account-mode gate runs in ``start()`` ahead of the barrier.
      Neither has a retry above it, so each owns its own refusal: the mode gate
      builds the retry loop it needs and then raises, ``universe.py`` raises
      outright. Both fault the startup barrier rather than freezing it.
    - The **same mode read in flight** (``preflight.reverify_account_mode``, ADR-0046
      §4) is the default half of that rule reached from the other side: the
      accounting cadence retries it, so it comes here and answers a
      ``VenueReadFailure`` as its ``UNREADABLE`` verdict. One read, two policies,
      chosen by what is above each caller rather than by the endpoint.

    Two policies, and the line between them is the retry, which is why a future
    in-flight read inherits this one by default and a new pre-barrier read does
    not.
    """
    try:
        response = await send(query)
    except OSError as exc:
        failed_send(request=request, cloid=cloid, error=exc)
        return VenueReadFailure.SEND_FAILED
    try:
        return normalize(response)
    except UNREADABLE as exc:
        unreadable_body(request=request, cloid=cloid, error=exc, response=response)
        return VenueReadFailure.UNREADABLE_BODY


def failed_send(*, request: str, cloid: str | None = None, error: OSError) -> None:
    """Name a request whose send died: no body arrived, so nothing was parsed.

    Public because the **write** verbs fail this way too. ``place`` and
    ``cancel`` send a signed action rather than a query, so they cannot go
    through ``read`` — but what a dead transport *means* must not be theirs to
    decide again, which is the per-site drift owning the taxonomy once exists to
    end (ADR-0048 §4).
    """
    _failed_read(request, cloid, str(error))


def unreadable_body(
    *, request: str, cloid: str | None = None, error: Exception, response: object
) -> None:
    """Name a body that arrived and could not be read, quoting what arrived.

    The body is not optional garnish: an unreadable body is how a venue contract
    change presents, so it repeats every cycle for as long as the contract stays
    broken, and an operator holding only the exception has nothing to act on.
    ``rendered`` leads with the key set for that reason. The write verbs used to
    name their half without it — the same failure, diagnosable on one path and
    not the other.
    """
    _failed_read(request, cloid, f"{error!r} in {request} response {rendered(response)}")


def _failed_read(request: str, cloid: str | None, error: str) -> None:
    """Name a read that yielded no usable answer, either way it failed.

    One event for both causes because the caller's verdict is one verdict — a
    frozen cycle, nothing healed — and ``error`` carries which it was. ``cloid``
    rides along only where the request has one, so the account grain omits it
    rather than logging a ``None`` that reads as a missing value.

    Private, with the two causes above as the way in: a caller that reached this
    directly would be choosing its own words for a failure the taxonomy already
    has words for, which is how the write path drifted from the read path in the
    first place.
    """
    context = {"cloid": cloid} if cloid is not None else {}
    named_event(NamedEvent.EXCHANGE_REQUEST_FAILED, request=request, error=error, **context)


_RENDER_LIMIT = 300
"""Characters of response body a failed read carries. Enough for the value-shaped
failures; short of a fifty-position body."""


def rendered(response: object) -> str:
    """``response`` bounded for a log line — its shape first, its body truncated.

    An unreadable body is what a venue contract change arrives as, so it repeats
    on every cycle for as long as the contract stays broken: a fifty-position
    body would be kilobytes an operator does not need served fifty times over.
    The **key set** is what identifies a contract change, so it leads and is
    never truncated; the body follows, bounded, for the failures a key set cannot
    show — a figure that is not a number.

    Nothing in here may raise: it runs only on the path that has already decided
    to answer ``UNREADABLE_BODY``, and an exception escaping would turn a
    fail-closed read into a crashed one. Hence ``list`` over ``sorted`` on the
    keys — mixed key types have no order but always have a repr.
    """
    shape = f"keys={list(response)} " if isinstance(response, Mapping) else ""
    body = repr(response)
    if len(body) > _RENDER_LIMIT:
        body = f"{body[:_RENDER_LIMIT]}… ({len(body)} chars)"
    return f"{shape}{body}"


def refuse_non_usdc(*, reported: str, row: str) -> NoReturn:
    """Refuse a money figure this venue settled in something other than USDC.

    Two grains reach here and their *detections* have nothing in common, which is
    why each stays with its own read: a fill carries a `feeToken` to compare,
    while a funding record carries no discriminator at all — its amount field is
    *named* `usdc`, so the denomination is the key itself and a payment settled
    elsewhere arrives with no `usdc` in it. What the two share is everything
    after that: the same reason there is nowhere to put the figure (ADR-0029
    leaves USDC implicit in a bare `Decimal`, so accruing it would add one
    currency to a line of another with nothing recording which), the same
    exception, and the same sentence to an operator. Saying it twice is how the
    two drift into disagreeing about a rule neither of them owns.

    `VenueFactUnsupported`, and deliberately not a member of `UNREADABLE`
    (ADR-0048): a settled venue row never changes, so the condition is known
    permanent at the first read. The transient verdict exists to find out — by
    waiting — whether a condition is durable, and there is nothing here to find
    out. ``reported`` leads with what the venue actually sent, because that is
    the one thing an operator's first question turns on.
    """
    raise VenueFactUnsupported(
        f"{reported}: this engine's money is a bare Decimal with USDC implicit "
        f"(ADR-0029), so it has no home in the ledger. Retrying cannot change a "
        f"settled {row} row."
    )


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
    read is a named ``VenueReadFailure``, an account read freezes the reconcile.
    This raises into ``UNREADABLE``, which every one of them already catches, so
    it adds a check and never a control flow.
    """
    if not isinstance(reported, str):
        raise TypeError(f"non-string figure {reported!r}")
    return exact_figure(Decimal(reported))
